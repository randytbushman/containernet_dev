"""
netexec.py: execution strategies for network-configuration commands.

Mininet configures interfaces, addresses, routes and traffic control by
running ``ip``, ``tc`` and ``ethtool`` inside each node. ``ip`` and ``tc``
come from iproute2, the standard Linux networking tools; ``tc`` (traffic
control) sets the queueing disciplines that add delay, loss and bandwidth
limits; ``ethtool`` changes NIC offload settings. For classic Mininet hosts
that works because every node shares the host's filesystem and therefore
the host's binaries. Containers do not: a minimal Alpine, Debian-slim or
distroless-style image frequently ships without iproute2, net-tools or
ethtool.

A container's interfaces live in its own network namespace (the kernel
object that gives a process its private set of interfaces, addresses and
routes). Any process that joins that namespace sees the container's
network, wherever its program file came from. This module therefore
decouples where a networking command runs from what it does. Each node
owns one runner (Node.netRunner, created by Node.createNetRunner()), and
Node.netCmd(), Intf.netCmd() and Intf.netQuery() send ip, tc and ethtool
commands through it.

The module contains:

- NetCommandRunner, the abstract strategy used by nodes and interfaces.
- NodeShellRunner, which runs commands through the node's own shell, as
  classic Mininet does.
- NamespaceRunner, which runs the host's binaries inside a process' network
  namespace via ``nsenter``. The container image needs no networking tools
  at all, and commands never contend with the node's interactive shell.
  Docker nodes use it, with a NodeShellRunner as fallback.
- IfconfigTranslator, which maps legacy ``ifconfig`` arguments onto
  iproute2 commands, so that Intf.ifconfig() works without net-tools.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Sequence

from mininet.log import debug

if TYPE_CHECKING:
    from mininet.node import Node


def joinCommand( args: Sequence[ object ] ) -> str:
    """Join Node.cmd()-style arguments into a single command string.

       Node.cmd() accepts either one list of arguments or the arguments
       themselves; both forms are joined with single spaces. Arguments are
       converted with str() but not quoted.

       :param args: a single string, a single list or tuple, or several
                    arguments
       :returns: the command as one string"""
    if len( args ) == 1 and isinstance( args[ 0 ], ( list, tuple ) ):
        args = args[ 0 ]
    return ' '.join( str( arg ) for arg in args )


class NetCommandRunner( ABC ):
    """Strategy that executes networking commands for one node.

       Subclasses decide where a command runs (the node's shell, or a host
       process inside the node's network namespace). Callers only use
       run(), query() and available().

       :ivar _toolCache: tool name to availability, filled by available()"""

    def __init__( self ) -> None:
        """Initialise the per-runner tool availability cache."""
        self._toolCache: dict[ str, bool ] = {}

    @abstractmethod
    def run( self, *args: object ) -> str:
        """Run a command that may change network state.

           :param args: command and arguments (same conventions as
                        Node.cmd(), see joinCommand())
           :returns: merged stdout and stderr of the command"""

    def query( self, *args: object ) -> str:
        """Run a read-only command whose output is parsed by the caller.

           Defaults to run(); subclasses may use a side channel that cannot
           interleave with output from the node's interactive shell.

           :param args: command and arguments
           :returns: merged stdout and stderr of the command"""
        return self.run( *args )

    def available( self, tool: str ) -> bool:
        """Return whether this runner can execute a tool.

           The answer is cached per runner, so each tool is probed at most
           once.

           :param tool: executable name, e.g. 'ethtool'
           :returns: True if the tool can be run"""
        if tool not in self._toolCache:
            self._toolCache[ tool ] = self._probe( tool )
        return self._toolCache[ tool ]

    @abstractmethod
    def _probe( self, tool: str ) -> bool:
        """Check, without the cache, whether a tool is available.

           :param tool: executable name
           :returns: True if the tool can be run"""


class NodeShellRunner( NetCommandRunner ):
    """Run networking commands through the node's own shell.

       This is classic Mininet behaviour: the tools must exist where the
       node's shell runs (for a container, inside the image).

       :ivar node: node whose shell executes the commands"""

    def __init__( self, node: Node ) -> None:
        """Create a runner for one node.

           :param node: node whose shell executes the commands"""
        super().__init__()
        self.node = node

    def run( self, *args: object ) -> str:
        """Run a command in the node's interactive shell.

           Shell syntax (pipes, redirection, ';') is interpreted.

           :param args: command and arguments
           :returns: command output ('' if the node's shell is gone)"""
        return self.node.cmd( *args ) or ''

    def query( self, *args: object ) -> str:
        """Run a read-only command in a separate process attached to the
           node.

           Using Node.pexec() instead of the interactive shell means that
           pending output of backgrounded shell jobs is not consumed or
           mixed into the result.

           :param args: command and arguments
           :returns: merged stdout and stderr ('' if pexec() failed)"""
        result = self.node.pexec( joinCommand( args ) )
        if result is None:
            return ''
        out, err, _code = result
        return out + err

    def _probe( self, tool: str ) -> bool:
        """Check whether the node's shell can resolve a tool.

           ``command -v`` prints the tool's path, or nothing if it is not
           found.

           :param tool: executable name
           :returns: True if the shell found the tool"""
        return bool( self.run( 'command -v %s' % shlex.quote( tool ) )
                     .strip() )


class NamespaceRunner( NetCommandRunner ):
    """Run host binaries inside another process' network namespace.

       This is how Containernet configures container networking: the
       container only provides the namespace, while ip, tc and ethtool come
       from the host. ``nsenter --target PID --net`` starts a program in
       the network namespace of process PID (the container's main
       process). Entering a namespace needs root, so the runner falls back
       to another runner when nsenter cannot be used (not root, nsenter
       missing, process gone).

       :ivar pidFn: returns the PID whose network namespace is entered
       :ivar fallback: runner used when nsenter is unusable, or None"""

    #: Path of the host's nsenter binary, looked up once at import time;
    #: None if it is not installed
    NSENTER: str | None = shutil.which( 'nsenter' )

    def __init__( self, pidFn: Callable[ [], int ],
                  fallback: NetCommandRunner | None = None ) -> None:
        """Create a runner for one node's namespace.

           The PID is read through a function on every call because a
           container's main PID changes when it is restarted.

           :param pidFn: returns the PID whose network namespace is entered
           :param fallback: runner used when nsenter is unusable; without
                            one, commands return an error message instead"""
        super().__init__()
        self.pidFn = pidFn
        self.fallback = fallback

    def usable( self ) -> bool:
        """Return whether nsenter can enter the target namespace now.

           :returns: True if nsenter is installed, we are root and the
                     target process has a network namespace in /proc"""
        pid = self.pidFn()
        return ( self.NSENTER is not None and os.geteuid() == 0
                 and pid is not None and pid > 0
                 and os.path.exists( '/proc/%d/ns/net' % pid ) )

    def run( self, *args: object ) -> str:
        """Run a command in the target network namespace.

           The command is split into an argument list with shlex and
           executed directly, without a shell, so shell syntax (pipes, ';',
           redirection) is not interpreted. Read-only commands use the same
           path (query() is not overridden) because each command already
           runs in its own process.

           :param args: command and arguments
           :returns: merged stdout and stderr of the command, or an error
                     message if it could not be started"""
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

           :param tool: executable name
           :returns: True if the tool is on the host's PATH while nsenter
                     is usable, otherwise whatever the fallback reports"""
        if self.usable():
            return shutil.which( tool ) is not None
        return self.fallback is not None and self.fallback.available( tool )


class IfconfigTranslator( object ):
    """Translate legacy ifconfig argument lists into iproute2 commands.

       Mininet and older topology code configure interfaces with ifconfig
       (from net-tools), which many images lack. The supported forms can
       be combined in any order:

       - No arguments shows the interface (``ip address show dev INTF``).
       - ``up`` or ``down`` sets the link state (``ip link set dev INTF
         up``).
       - ``hw ether MAC`` sets the MAC address (``ip link set dev INTF
         address MAC``).
       - ``mtu N`` sets the MTU (``ip link set dev INTF mtu N``).
       - An IPv4 address, either as ``ADDR/LEN`` or as ``ADDR`` with an
         optional ``netmask MASK`` (32 bits if neither is given), replaces
         all IPv4 addresses of the interface (``ip -4 address flush`` then
         ``ip address add``).

       Anything else makes translate() return None so the caller can run a
       real ifconfig instead."""

    @staticmethod
    def _isIPv4( token: str ) -> bool:
        """Return whether a token is an IPv4 address, optionally with /len.

           :param token: one ifconfig argument
           :returns: True if the ipaddress module accepts it"""
        try:
            ipaddress.IPv4Interface( token )
            return True
        except ValueError:
            return False

    @classmethod
    def translate( cls, intf: str,
                   args: Sequence[ str ] ) -> list[ list[ str ] ] | None:
        """Translate ifconfig arguments for one interface.

           Commands are returned in a fixed order regardless of the order
           of the arguments: address changes first, then link settings
           (MAC, MTU), then the up or down state.

           :param intf: interface name
           :param args: ifconfig arguments following the interface name
           :returns: list of argv lists to execute in order, or None if the
                     arguments are not understood (including a netmask
                     without an address)"""
        if not args:
            return [ [ 'ip', 'address', 'show', 'dev', intf ] ]
        address: str | None = None
        netmask: str | None = None
        # Arguments for 'ip link set' other than the state
        linkArgs: list[ str ] = []
        state: str | None = None
        # Walk the tokens; options with values consume them as well
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
            # An address without /len takes its prefix from the netmask
            # (a /32 host address if there is none)
            try:
                address = ipaddress.IPv4Interface(
                    address if '/' in address
                    else ( address, netmask or 32 ) ).with_prefixlen
            except ValueError:
                return None
            # ifconfig replaces the interface's address, so drop the old
            # IPv4 addresses first (IPv6 addresses are kept)
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
