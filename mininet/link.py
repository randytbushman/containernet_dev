"""
link.py: interface and link abstractions for mininet

It seems useful to bundle functionality for interfaces into a single
class.

Also it seems useful to enable the possibility of multiple flavors of
links, including:

- simple veth pairs
- tunneled links
- patchable links (which can be disconnected and reconnected via a patchbay)
- link simulators (e.g. wireless)

Basic division of labor:

  Nodes: know how to execute commands
  Intfs: know how to configure themselves
  Links: know how to connect nodes together

Intf: basic interface object that can configure itself
TCIntf: interface with bandwidth limiting and delay via tc

Link: basic link class for creating veth pairs
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from mininet.log import info, error, debug
from mininet.netexec import IfconfigTranslator
from mininet.util import makeIntfPair

# Linux limits interface names to IFNAMSIZ - 1 characters
MAX_INTF_NAME_LEN = 15

# Make pylint happy:
# pylint: disable=too-many-arguments


class Intf( object ):

    """Basic interface object that can configure itself.

       All configuration goes through the owning node's network command
       runner (node.netCmd), so it works even when a container image ships
       neither iproute2 nor net-tools."""

    _ipMatchRegex = re.compile( r'\d+\.\d+\.\d+\.\d+' )
    _macMatchRegex = re.compile( r'..:..:..:..:..:..' )
    _inetRegex = re.compile( r'\binet (\d+\.\d+\.\d+\.\d+)/(\d+)' )
    _etherRegex = re.compile(
        r'link/ether ((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})' )
    _flagsRegex = re.compile( r'<([^>]*)>' )

    def __init__( self, name, node=None, port=None, link=None,
                  mac=None, **params ):
        """name: interface name (e.g. h1-eth0)
           node: owning node (where this intf most likely lives)
           link: parent link if we're part of a link
           other arguments are passed to config(); new interfaces are
           brought up unless up=False is given"""
        self.node = node
        self.name = name
        self.link = link
        self.mac = mac
        self.ip, self.prefixLen = None, None
        self._ifconfigOverride: Optional[ Callable[ ..., str ] ] = None

        # if interface is lo, we know the ip is 127.0.0.1.
        # This saves a command per node
        if self.name == 'lo':
            self.ip = '127.0.0.1'
            self.prefixLen = 8
        # Add to node (and move ourselves if necessary )
        if node:
            moveIntfFn = params.pop( 'moveIntfFn', None )
            if moveIntfFn:
                node.addIntf( self, port=port, moveIntfFn=moveIntfFn )
            else:
                node.addIntf( self, port=port )
        # Only the initial configuration brings the interface up; later
        # config() calls leave the administrative state untouched.
        params.setdefault( 'up', True )
        # Save params for future reference
        self.params = params
        self.config( **params )

    def cmd( self, *args, **kwargs ):
        "Run a command in our owning node"
        return self.node.cmd( *args, **kwargs )

    def netCmd( self, *args: object ) -> str:
        """Run a networking command (ip/tc/ethtool) in our node's network
           namespace using the node's runner strategy.
           args: command and arguments
           returns: command output"""
        return self.node.netCmd( *args )

    def netQuery( self, *args: object ) -> str:
        """Run a read-only networking command in our node's network
           namespace without disturbing the node's interactive shell.
           args: command and arguments
           returns: command output"""
        return self.node.netRunner.query( *args )

    def ifconfig( self, *args: object ) -> str:
        """Configure ourselves using ifconfig-style arguments.
           Arguments are translated to iproute2, so images without
           net-tools work. If a callable was passed as the 'ifconfig'
           parameter, it is called as fn( intf, *args ) instead.
           args: e.g. 'up', 'down', 'hw', 'ether', <mac>
           returns: command output"""
        if self._ifconfigOverride is not None:
            return self._ifconfigOverride( self, *args ) or ''
        tokens = [ str( arg ) for arg in args ]
        commands = IfconfigTranslator.translate( self.name, tokens )
        if commands is None:
            # Unknown option: defer to a real ifconfig binary
            return self.netCmd( 'ifconfig', self.name, *tokens )
        return ''.join( self.netCmd( *argv ) for argv in commands )

    def setIP( self, ipstr: str, prefixLen: Optional[ int ] = None,
               overwrite: bool = True ) -> str:
        """Set our IPv4 address.
           ipstr: address, optionally with '/prefixLen'
           prefixLen: prefix length if ipstr has none
           overwrite: remove existing IPv4 addresses first (IPv6,
                      including the link-local address, is preserved)
           returns: command output"""
        if '/' in ipstr:
            ip, prefix = ipstr.split( '/', 1 )
        elif prefixLen is None:
            raise ValueError( 'No prefix length set for IP address %s'
                              % ( ipstr, ) )
        else:
            ip, prefix = ipstr, prefixLen
        self.ip, self.prefixLen = ip, int( prefix )
        if overwrite:
            self.netCmd( 'ip', '-4', 'address', 'flush', 'dev', self.name )
        return self.netCmd( 'ip', 'address', 'add',
                            '%s/%s' % ( self.ip, self.prefixLen ),
                            'dev', self.name )

    def setMAC( self, macstr: str ) -> str:
        """Set the MAC address for an interface. The interface is briefly
           taken down and restored to its previous state.
           macstr: MAC address as string
           returns: command output"""
        self.mac = macstr
        wasUp = self.get_state() == 'up'
        result = self.netCmd( 'ip', 'link', 'set', 'dev', self.name, 'down' )
        result += self.netCmd( 'ip', 'link', 'set', 'dev', self.name,
                               'address', macstr )
        if wasUp:
            result += self.netCmd( 'ip', 'link', 'set', 'dev', self.name,
                                   'up' )
        return result

    def updateIP( self ) -> Optional[ str ]:
        "Return updated IP address based on the kernel's address list"
        self.updateAddr()
        return self.ip

    def updateMAC( self ) -> Optional[ str ]:
        "Return updated MAC address based on the kernel's link info"
        self.updateAddr()
        return self.mac

    def updateAddr( self ) -> tuple:
        """Return IP address and MAC address using a single query.
           returns: ( ip, mac ), either may be None"""
        output = self.netQuery( 'ip', 'address', 'show', 'dev', self.name )
        inet = self._inetRegex.search( output )
        ether = self._etherRegex.search( output )
        self.ip, self.prefixLen = ( ( inet.group( 1 ), int( inet.group( 2 ) ) )
                                    if inet else ( None, None ) )
        self.mac = ether.group( 1 ) if ether else None
        return self.ip, self.mac

    def IP( self ):
        "Return IP address"
        return self.ip

    def MAC( self ):
        "Return MAC address"
        return self.mac

    def get_state( self ) -> str:
        """Return the administrative state of the interface.
           returns: 'up' or 'down'"""
        output = self.netQuery( 'ip', '-o', 'link', 'show', 'dev', self.name )
        flags = self._flagsRegex.search( output )
        if flags and 'UP' in flags.group( 1 ).split( ',' ):
            return 'up'
        return 'down'

    def set_state( self, state: str ) -> str:
        """Set the administrative state of the interface.
           state: 'up' or 'down'
           returns: command output ('' on success)"""
        if state not in ( 'up', 'down' ):
            raise ValueError( "Invalid state %r; expected 'up' or 'down'"
                              % ( state, ) )
        return self.ifconfig( state ) or ''

    def isUp( self, setUp: bool = False ) -> bool:
        """Return whether interface is up.
           setUp: bring the interface up first"""
        if setUp:
            cmdOutput = self.set_state( 'up' )
            # no output indicates success
            if cmdOutput.strip():
                error( "Error setting %s up: %s " % ( self.name, cmdOutput ) )
                return False
            return True
        return self.get_state() == 'up'

    def rename( self, newname: str ) -> str:
        """Rename interface, preserving its administrative state.
           newname: new interface name (max 15 characters)
           returns: command output"""
        wasUp = self.get_state() == 'up'
        self.netCmd( 'ip', 'link', 'set', 'dev', self.name, 'down' )
        result = self.netCmd( 'ip', 'link', 'set', 'dev', self.name,
                              'name', newname )
        if self.node and self.name in self.node.nameToIntf:
            # rename intf in node's nameToIntf
            self.node.nameToIntf[newname] = self.node.nameToIntf.pop(self.name)
        self.name = newname
        if wasUp:
            self.netCmd( 'ip', 'link', 'set', 'dev', self.name, 'up' )
        return result

    # The reason why we configure things in this way is so
    # That the parameters can be listed and documented in
    # the config method.
    # Dealing with subclasses and superclasses is slightly
    # annoying, but at least the information is there!

    def setParam( self, results, method, **param ):
        """Internal method: configure a *single* parameter
           results: dict of results to update
           method: config method name
           param: arg=value (ignore if value=None)
           value may also be list or dict"""
        name, value = list( param.items() )[ 0 ]
        f = getattr( self, method, None )
        if not f or value is None:
            return None
        if isinstance( value, list ):
            result = f( *value )
        elif isinstance( value, dict ):
            result = f( **value )
        else:
            result = f( value )
        results[ name ] = result
        return result

    def config( self, mac=None, ip=None, ifconfig=None,
                up=None, **_params ):
        """Configure Node according to (optional) parameters:
           mac: MAC address
           ip: IP address
           ifconfig: ifconfig-style arguments to apply, or a callable
                     fn( intf, *args ) that replaces ifconfig() for this
                     interface from now on
           up: True brings the interface up, None leaves it unchanged
           Subclasses should override this method and call
           the parent class's config(**params)"""
        # If we were overriding this method, we would call
        # the superclass config method here as follows:
        # r = Parent.config( **params )
        r = {}
        if callable( ifconfig ):
            self._ifconfigOverride = ifconfig
            ifconfig = None
        self.setParam( r, 'setMAC', mac=mac )
        self.setParam( r, 'setIP', ip=ip )
        if up:
            self.setParam( r, 'isUp', up=up )
        self.setParam( r, 'ifconfig', ifconfig=ifconfig )
        return r

    def delete( self ):
        "Delete interface"
        self.netCmd( 'ip', 'link', 'del', 'dev', self.name )
        # We used to do this, but it slows us down:
        # if self.node.inNamespace:
        # Link may have been dumped into root NS
        # quietRun( 'ip link del ' + self.name )
        self.node.delIntf( self )
        self.link = None

        # call detach if we have a OVSSwitch (just to be sure);
        # imported lazily because mininet.node imports this module
        from mininet.node import OVSSwitch
        if isinstance( self.node, OVSSwitch ):
            self.node.detach(self)

    def status( self ) -> str:
        "Return intf status as a string"
        links = self.netQuery( 'ip', '-o', 'link', 'show' )
        pattern = r'^\d+:\s+%s[@:]' % re.escape( self.name )
        return "OK" if re.search( pattern, links, re.MULTILINE ) else "MISSING"

    def __repr__( self ):
        return '<%s %s>' % ( self.__class__.__name__, self.name )

    def __str__( self ):
        return self.name


class TCIntf( Intf ):
    """Interface customized by tc (traffic control) utility
       Allows specification of bandwidth limits (various methods)
       as well as delay, loss and max queue length"""

    # The parameters we use seem to work reasonably up to 1 Gb/sec
    # For higher data rates, we will probably need to change them.
    bwParamMax = 1000

    _rootQdiscRegex = re.compile( r'qdisc \S+ ([0-9a-f]+): root' )

    # Harmless tc complaints, e.g. when deleting an absent qdisc
    _ignoredTcErrors = ( 'No such file or directory',
                         'Cannot delete qdisc with handle of zero' )

    @staticmethod
    def _number( value: object, name: str ) -> Optional[ float ]:
        """Normalise a numeric shaping parameter.
           Accepts numbers or strings such as '10', '1.5' or '1%'.
           value: parameter value or None
           name: parameter name for error messages
           returns: float or None"""
        if value is None or isinstance( value, ( int, float ) ):
            return value
        try:
            return float( str( value ).strip().rstrip( '%' ) )
        except ValueError:
            raise ValueError( 'Invalid %s value: %r' % ( name, value ) )

    def bwCmds( self, bw=None, speedup=0, use_hfsc=False, use_tbf=False,
                latency_ms=None, enable_ecn=False, enable_red=False ):
        "Return tc commands to set bandwidth"

        cmds, parent = [], ' root '

        if bw is not None and ( bw <= 0 or bw > self.bwParamMax ):
            error( 'Bandwidth limit', bw, 'is outside supported range '
                   '(0..%d] - ignoring\n' % self.bwParamMax )
        elif bw is not None:
            # BL: this seems a bit brittle...
            if ( speedup > 0 and
                 self.node.name[0:1] == 's' ):
                bw = speedup
            # This may not be correct - we should look more closely
            # at the semantics of burst (and cburst) to make sure we
            # are specifying the correct sizes. For now I have used
            # the same settings we had in the mininet-hifi code.
            if use_hfsc:
                cmds += [ '%s qdisc add dev %s root handle 5:0 hfsc default 1',
                          '%s class add dev %s parent 5:0 classid 5:1 hfsc sc '
                          + 'rate %fMbit ul rate %fMbit' % ( bw, bw ) ]
            elif use_tbf:
                if latency_ms is None:
                    latency_ms = 15.0 * 8 / bw
                cmds += [ '%s qdisc add dev %s root handle 5: tbf ' +
                          'rate %fMbit burst 15000 latency %fms' %
                          ( bw, latency_ms ) ]
            else:
                cmds += [ '%s qdisc add dev %s root handle 5:0 htb default 1',
                          '%s class add dev %s parent 5:0 classid 5:1 htb ' +
                          'rate %fMbit burst 15k' % bw ]
            parent = ' parent 5:1 '

            # ECN or RED
            if enable_ecn:
                cmds += [ '%s qdisc add dev %s' + parent +
                          'handle 6: red limit 1000000 ' +
                          'min 30000 max 35000 avpkt 1500 ' +
                          'burst 20 ' +
                          'bandwidth %fmbit probability 1 ecn' % bw ]
                parent = ' parent 6: '
            elif enable_red:
                cmds += [ '%s qdisc add dev %s' + parent +
                          'handle 6: red limit 1000000 ' +
                          'min 30000 max 35000 avpkt 1500 ' +
                          'burst 20 ' +
                          'bandwidth %fmbit probability 1' % bw ]
                parent = ' parent 6: '
        return cmds, parent

    @staticmethod
    def delayCmds( parent, delay=None, jitter=None,
                   loss=None, max_queue_size=None ):
        "Internal method: return tc commands for delay and loss"
        cmds = []
        if loss is not None and ( loss < 0 or loss > 100 ):
            error( 'Bad loss percentage', loss, '%\n' )
            return cmds, parent
        if jitter is not None and delay is None:
            # netem only accepts jitter as a modifier of a delay
            error( 'Jitter', jitter, 'requires a delay - ignoring jitter\n' )
            jitter = None
        # Delay/jitter/loss/max queue size
        netemargs = '%s%s%s%s' % (
            'delay %s ' % delay if delay is not None else '',
            '%s ' % jitter if jitter is not None else '',
            'loss %.5f ' % loss if (loss is not None and loss > 0) else '',
            'limit %d' % max_queue_size if max_queue_size is not None
            else '' )
        if netemargs:
            cmds = [ '%s qdisc add dev %s ' + parent +
                     ' handle 10: netem ' +
                     netemargs ]
            parent = ' parent 10:1 '
        return cmds, parent

    def tc( self, cmd, tc='tc' ):
        "Execute tc command for our interface"
        c = cmd % (tc, self)  # Add in tc command and our name
        debug(" *** executing command: %s\n" % c)
        return self.netCmd( c )

    def rootQdiscHandle( self ) -> Optional[ str ]:
        """Return the handle of the root qdisc, e.g. '5' or '0' for the
           kernel default (noqueue, pfifo_fast, fq_codel...).
           returns: handle string or None if it cannot be determined"""
        match = self._rootQdiscRegex.search( self.tc( '%s qdisc show dev %s' ) )
        return match.group( 1 ) if match else None

    # pylint: disable=arguments-differ
    def config( self, bw=None, delay=None, jitter=None, loss=None,
                gro=False, txo=True, rxo=True,
                speedup=0, use_hfsc=False, use_tbf=False,
                latency_ms=None, enable_ecn=False, enable_red=False,
                max_queue_size=None, **params ):
        """Configure the port and set its properties.
           Every call fully describes the desired shaping: parameters that
           are omitted are removed, and a call without any shaping
           parameters restores the kernel's default qdisc.
           bw: bandwidth in Mbit/s (e.g. 10)
           delay: transmit delay (e.g. '1ms' )
           jitter: jitter (e.g. '1ms'); requires delay
           loss: loss percentage (e.g. 1 or '1%' )
           gro: enable GRO (False)
           txo: enable transmit checksum offload (True)
           rxo: enable receive checksum offload (True)
           speedup: experimental switch-side bw option
           use_hfsc: use HFSC scheduling
           use_tbf: use TBF scheduling
           latency_ms: TBF latency parameter
           enable_ecn: enable ECN (False)
           enable_red: enable RED (False)
           max_queue_size: queue limit parameter for netem"""

        # Support old names for parameters
        gro = not params.pop( 'disable_gro', not gro )
        bw = self._number( bw, 'bw' )
        loss = self._number( loss, 'loss' )

        result = Intf.config( self, **params)

        def on( isOn ):
            "Helper method: bool -> 'on'/'off'"
            return 'on' if isOn else 'off'

        # Set offload parameters with ethtool (skipped if not installed)
        if self.node.netRunner.available( 'ethtool' ):
            self.netCmd( 'ethtool -K', self,
                         'gro', on( gro ),
                         'tx', on( txo ),
                         'rx', on( rxo ) )

        # Remove any shaping we (or anyone) installed earlier so this call
        # fully determines the result; default qdiscs have handle 0
        cmds = []
        if self.rootQdiscHandle() not in ( None, '0' ):
            cmds.append( '%s qdisc del dev %s root' )

        # Bandwidth limits via various methods
        bwcmds, parent = self.bwCmds( bw=bw, speedup=speedup,
                                      use_hfsc=use_hfsc, use_tbf=use_tbf,
                                      latency_ms=latency_ms,
                                      enable_ecn=enable_ecn,
                                      enable_red=enable_red )
        cmds += bwcmds

        # Delay/jitter/loss/max_queue_size using netem
        delaycmds, parent = self.delayCmds( delay=delay, jitter=jitter,
                                            loss=loss,
                                            max_queue_size=max_queue_size,
                                            parent=parent )
        cmds += delaycmds

        # Optimization: nothing installed and nothing requested
        if not cmds:
            return None

        # Ugly but functional: display configuration info
        stuff = ( ( [ '%.2fMbit' % bw ] if bw is not None else [] ) +
                  ( [ '%s delay' % delay ] if delay is not None else [] ) +
                  ( [ '%s jitter' % jitter ] if jitter is not None else [] ) +
                  ( ['%.5f%% loss' % loss ] if loss is not None else [] ) +
                  ( [ 'ECN' ] if enable_ecn else [ 'RED' ]
                    if enable_red else [] ) )
        info( '(' + ' '.join( stuff ) + ') ' )

        # Execute all the commands in our node
        debug("at map stage w/cmds: %s\n" % cmds)
        tcoutputs = [ self.tc(cmd) for cmd in cmds ]
        for output in tcoutputs:
            if output.strip() and not any( msg in output for msg
                                           in self._ignoredTcErrors ):
                error( "*** Error: %s" % output )
        debug( "cmds:", cmds, '\n' )
        debug( "outputs:", tcoutputs, '\n' )
        result[ 'tcoutputs'] = tcoutputs
        result[ 'parent' ] = parent

        return result


class Link( object ):

    """A basic link is just a veth pair.
       Other types of links could be tunnels, link emulators, etc.."""

    # pylint: disable=too-many-branches
    def __init__( self, node1, node2, port1=None, port2=None,
                  intfName1=None, intfName2=None, addr1=None, addr2=None,
                  intf=Intf, cls1=None, cls2=None, params1=None,
                  params2=None, fast=True, **params ):
        """Create veth link to another node, making two new interfaces.
           node1: first node
           node2: second node
           port1: node1 port number (optional)
           port2: node2 port number (optional)
           intf: default interface class/constructor
           cls1, cls2: optional interface-specific constructors
           intfName1: node1 interface name (optional)
           intfName2: node2  interface name (optional)
           params1: parameters for interface 1 (optional)
           params2: parameters for interface 2 (optional)
           **params: additional parameters for both interfaces"""

        # This is a bit awkward; it seems that having everything in
        # params is more orthogonal, but being able to specify
        # in-line arguments is more convenient! So we support both.
        params1 = dict( params1 ) if params1 else {}
        params2 = dict( params2 ) if params2 else {}
        if port1 is not None:
            params1[ 'port' ] = port1
        if port2 is not None:
            params2[ 'port' ] = port2
        if 'port' not in params1:
            params1[ 'port' ] = node1.newPort()
        if 'port' not in params2:
            params2[ 'port' ] = node2.newPort()
        if not intfName1:
            intfName1 = self.intfName( node1, params1[ 'port' ] )
        if not intfName2:
            intfName2 = self.intfName( node2, params2[ 'port' ] )

        # Update with remaining parameter list
        params1.update( params )
        params2.update( params )

        self.fast = fast
        if fast:
            params1.setdefault( 'moveIntfFn', self._ignore )
            params2.setdefault( 'moveIntfFn', self._ignore )
            self.makeIntfPair( intfName1, intfName2, addr1, addr2,
                               node1, node2, deleteIntfs=False )
        else:
            self.makeIntfPair( intfName1, intfName2, addr1, addr2 )

        if not cls1:
            cls1 = intf
        if not cls2:
            cls2 = intf

        intf1 = cls1( name=intfName1, node=node1,
                      link=self, mac=addr1, **params1  )
        intf2 = cls2( name=intfName2, node=node2,
                      link=self, mac=addr2, **params2 )

        # All we are is dust in the wind, and our two interfaces
        self.intf1, self.intf2 = intf1, intf2

    # pylint: enable=too-many-branches

    @staticmethod
    def _ignore( *args, **kwargs ):
        "Ignore any arguments"
        pass

    def intfName( self, node, n ):
        """Construct a canonical interface name node-ethN for interface n.
           raises: ValueError if the name exceeds the kernel's limit"""
        # Leave this as an instance method for now
        assert self
        name = node.name + '-eth' + repr( n )
        if len( name ) > MAX_INTF_NAME_LEN:
            raise ValueError(
                'Interface name %r is longer than %d characters; use a '
                'shorter node name or pass intfName1/intfName2'
                % ( name, MAX_INTF_NAME_LEN ) )
        return name

    @classmethod
    def makeIntfPair( cls, intfname1, intfname2, addr1=None, addr2=None,
                      node1=None, node2=None, deleteIntfs=True ):
        """Create pair of interfaces
           intfname1: name for interface 1
           intfname2: name for interface 2
           addr1: MAC address for interface 1 (optional)
           addr2: MAC address for interface 2 (optional)
           node1: home node for interface 1 (optional)
           node2: home node for interface 2 (optional)
           (override this method [and possibly delete()]
           to change link type)"""
        # Leave this as a class method for now
        assert cls
        return makeIntfPair( intfname1, intfname2, addr1, addr2, node1, node2,
                             deleteIntfs=deleteIntfs )

    def delete( self ):
        "Delete this link"
        self.intf1.delete()
        self.intf1 = None
        self.intf2.delete()
        self.intf2 = None

    def stop( self ):
        "Override to stop and clean up link as needed"
        self.delete()

    def status( self ):
        "Return link status as a string"
        return "(%s %s)" % ( self.intf1.status(), self.intf2.status() )

    def __str__( self ):
        return '%s<->%s' % ( self.intf1, self.intf2 )


class OVSIntf( Intf ):
    "Patch interface on an OVSSwitch"

    def ifconfig( self, *args ):
        cmd = ' '.join( args )
        if cmd == 'up':
            # OVSIntf is always up
            return
        else:
            raise Exception( 'OVSIntf cannot do ifconfig ' + cmd )


class OVSLink( Link ):
    """Link that makes patch links between OVSSwitches
       Warning: in testing we have found that no more
       than ~64 OVS patch links should be used in row."""

    def __init__( self, node1, node2, **kwargs ):
        "See Link.__init__() for options"
        try:
            OVSSwitch
        except NameError:
            # pylint: disable=import-outside-toplevel,cyclic-import
            from mininet.node import OVSSwitch
        self.isPatchLink = False
        if ( isinstance( node1, OVSSwitch ) and
             isinstance( node2, OVSSwitch ) ):
            self.isPatchLink = True
            kwargs.update( cls1=OVSIntf, cls2=OVSIntf )
        Link.__init__( self, node1, node2, **kwargs )

    # pylint: disable=arguments-differ, signature-differs
    def makeIntfPair( self, *args, **kwargs ):
        "Usually delegated to OVSSwitch"
        if self.isPatchLink:
            return None, None
        else:
            return Link.makeIntfPair( *args, **kwargs )


class TCLink( Link ):
    "Link with TC interfaces"
    def __init__( self, *args, **kwargs):
        kwargs.setdefault( 'cls1', TCIntf )
        kwargs.setdefault( 'cls2', TCIntf )
        Link.__init__( self, *args, **kwargs)


class TCULink( TCLink ):
    """TCLink with default settings optimized for UserSwitch
       (txo=rxo=0/False).  Unfortunately with recent Linux kernels,
       enabling TX and RX checksum offload on veth pairs doesn't work
       well with UserSwitch: either it gets terrible performance or
       TCP packets with bad checksums are generated, forwarded, and
       *dropped* due to having bad checksums! OVS and LinuxBridge seem
       to cope with this somehow, but it is likely to be an issue with
       many software Ethernet bridges."""

    def __init__( self, *args, **kwargs ):
        kwargs.update( txo=False, rxo=False )
        TCLink.__init__( self, *args, **kwargs )
