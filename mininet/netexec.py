"""
netexec.py: execution strategies for network-configuration commands.

Mininet configures interfaces, addresses, routes and traffic control by
running ``ip``, ``tc`` and ``ethtool`` *inside* each node. For classic
Mininet hosts that works because every node shares the host's filesystem.
Containers do not: a minimal Alpine, Debian-slim or distroless-style image
frequently ships without iproute2, net-tools or ethtool.

This module decouples *where* a networking command runs from *what* it does:

NetCommandRunner: abstract strategy used by nodes and interfaces.
NodeShellRunner: runs commands through the node's own shell (classic Mininet).
NamespaceRunner: runs the *host's* binaries inside a process' network
    namespace via ``nsenter``. The container image needs no networking tools
    at all, and commands never contend with the node's interactive shell.
IfconfigTranslator: maps legacy ``ifconfig`` arguments onto iproute2.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from mininet.log import debug

if TYPE_CHECKING:
    from mininet.node import Node


def joinCommand( args: Sequence[ object ] ) -> str:
    """Join Node.cmd()-style arguments into a single command string.
       args: a single string, a single list, or several arguments
       returns: command string"""
    if len( args ) == 1 and isinstance( args[ 0 ], ( list, tuple ) ):
        args = args[ 0 ]
    return ' '.join( str( arg ) for arg in args )


class NetCommandRunner( ABC ):
    """Strategy that executes networking commands for a node."""

    def __init__( self ) -> None:
        """Initialise the per-runner tool availability cache."""
        self._toolCache: dict[ str, bool ] = {}

    @abstractmethod
    def run( self, *args: object ) -> str:
        """Run a command that may change network state.
           args: command and arguments (same conventions as Node.cmd)
           returns: merged stdout/stderr of the command"""

    def query( self, *args: object ) -> str:
        """Run a read-only command whose output is parsed by the caller.
           Defaults to run(); subclasses may use a side channel that cannot
           interleave with output from the node's interactive shell.
           args: command and arguments
           returns: merged stdout/stderr of the command"""
        return self.run( *args )

    def available( self, tool: str ) -> bool:
        """Return whether a tool can be executed by this runner (cached).
           tool: executable name, e.g. 'ethtool'"""
        if tool not in self._toolCache:
            self._toolCache[ tool ] = self._probe( tool )
        return self._toolCache[ tool ]

    @abstractmethod
    def _probe( self, tool: str ) -> bool:
        """Uncached availability check for a tool.
           tool: executable name"""


class NodeShellRunner( NetCommandRunner ):
    """Run networking commands through the node's own shell."""

    def __init__( self, node: Node ) -> None:
        """node: node whose shell executes the commands"""
        super().__init__()
        self.node = node

    def run( self, *args: object ) -> str:
        """Run a command in the node's shell.
           args: command and arguments
           returns: command output ('' if the node's shell is gone)"""
        return self.node.cmd( *args ) or ''

    def query( self, *args: object ) -> str:
        """Run a read-only command in a separate process attached to the
           node, so pending output of backgrounded shell jobs is not
           consumed.
           args: command and arguments
           returns: merged stdout/stderr"""
        result = self.node.pexec( joinCommand( args ) )
        if result is None:
            return ''
        out, err, _code = result
        return out + err

    def _probe( self, tool: str ) -> bool:
        """Check whether the node's shell can resolve a tool.
           tool: executable name"""
        return bool( self.run( 'command -v %s' % shlex.quote( tool ) )
                     .strip() )


class NamespaceRunner( NetCommandRunner ):
    """Run host binaries inside another process' network namespace.

       This is how Containernet configures container networking: the
       container only provides the namespace, while ip/tc/ethtool come
       from the host. Falls back to another runner when nsenter cannot
       be used (not root, nsenter missing, process gone)."""

    NSENTER: Optional[ str ] = shutil.which( 'nsenter' )

    def __init__( self, pidFn: Callable[ [], int ],
                  fallback: Optional[ NetCommandRunner ] = None ) -> None:
        """pidFn: returns the PID whose network namespace is entered
           fallback: runner used when nsenter is unusable"""
        super().__init__()
        self.pidFn = pidFn
        self.fallback = fallback

    def usable( self ) -> bool:
        """Return whether nsenter can enter the target namespace now."""
        pid = self.pidFn()
        return ( self.NSENTER is not None and os.geteuid() == 0
                 and pid is not None and pid > 0
                 and os.path.exists( '/proc/%d/ns/net' % pid ) )

    def run( self, *args: object ) -> str:
        """Run a command in the target network namespace.
           Shell syntax (pipes, ';', redirection) is not interpreted.
           args: command and arguments
           returns: merged stdout/stderr of the command"""
        if not self.usable():
            if self.fallback is None:
                return 'nsenter unavailable for pid %s\n' % self.pidFn()
            return self.fallback.run( *args )
        argv = [ self.NSENTER, '--target', str( self.pidFn() ), '--net',
                 '--' ] + shlex.split( joinCommand( args ) )
        debug( '*** nsenter:', argv, '\n' )
        try:
            proc = subprocess.run( argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, check=False )
        except OSError as exc:
            return '%s\n' % exc
        return proc.stdout.decode( errors='replace' )

    def _probe( self, tool: str ) -> bool:
        """Check whether a tool exists on the host (or in the fallback).
           tool: executable name"""
        if self.usable():
            return shutil.which( tool ) is not None
        return self.fallback is not None and self.fallback.available( tool )


class IfconfigTranslator( object ):
    """Translate legacy ifconfig argument lists into iproute2 commands.

       Supported forms (combinable, in any order):
         (no arguments)            -> ip address show dev <intf>
         up | down                 -> ip link set dev <intf> up|down
         hw ether <mac>            -> ip link set dev <intf> address <mac>
         mtu <n>                   -> ip link set dev <intf> mtu <n>
         <ip>[/<len>] [netmask <m>] -> ip address replace <ip>/<len> ...
       Anything else returns None so the caller can use a real ifconfig."""

    @staticmethod
    def _isIPv4( token: str ) -> bool:
        """Return whether a token is an IPv4 address (optionally /len)."""
        try:
            ipaddress.IPv4Interface( token )
            return True
        except ValueError:
            return False

    @classmethod
    def translate( cls, intf: str,
                   args: Sequence[ str ] ) -> Optional[ list[ list[ str ] ] ]:
        """Translate ifconfig arguments for one interface.
           intf: interface name
           args: ifconfig arguments following the interface name
           returns: list of argv lists to execute in order, or None if the
                    arguments are not understood"""
        if not args:
            return [ [ 'ip', 'address', 'show', 'dev', intf ] ]
        address: Optional[ str ] = None
        netmask: Optional[ str ] = None
        linkArgs: list[ str ] = []
        state: Optional[ str ] = None
        i = 0
        while i < len( args ):
            token = args[ i ]
            if token in ( 'up', 'down' ):
                state = token
                i += 1
            elif ( token == 'hw' and i + 2 < len( args )
                   and args[ i + 1 ] == 'ether' ):
                linkArgs += [ 'address', args[ i + 2 ] ]
                i += 3
            elif token == 'mtu' and i + 1 < len( args ):
                linkArgs += [ 'mtu', args[ i + 1 ] ]
                i += 2
            elif token == 'netmask' and i + 1 < len( args ):
                netmask = args[ i + 1 ]
                i += 2
            elif address is None and cls._isIPv4( token ):
                address = token
                i += 1
            else:
                return None
        commands: list[ list[ str ] ] = []
        if address is not None:
            if '/' not in address:
                mask = netmask or '255.255.255.255'
                try:
                    prefix = ipaddress.IPv4Network(
                        '0.0.0.0/%s' % mask ).prefixlen
                except ValueError:
                    return None
                address = '%s/%d' % ( address, prefix )
            commands.append( [ 'ip', '-4', 'address', 'flush', 'dev', intf ] )
            commands.append( [ 'ip', 'address', 'add', address,
                               'dev', intf ] )
        elif netmask is not None:
            return None
        if linkArgs:
            commands.append( [ 'ip', 'link', 'set', 'dev', intf ] + linkArgs )
        if state is not None:
            commands.append( [ 'ip', 'link', 'set', 'dev', intf, state ] )
        return commands
