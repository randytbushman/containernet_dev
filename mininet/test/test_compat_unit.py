"""
Unit tests for the fork's container compatibility logic.

These tests need neither Docker nor root: they call the pure logic directly,
or replace nodes with mock objects. They cover ifconfig-to-iproute2
translation and the nsenter command runners (mininet/netexec.py), Docker
image reference parsing and control shell profiles (mininet/container.py),
tc (Linux traffic control) command generation in TCIntf, parsing of
iproute2 output in Intf, the shell prompt sentinel of Node, and helpers
from mininet/util.py and mininet/link.py.

Run from the containernet directory with::

    python3 -m unittest discover -s mininet/test -p 'test_compat_unit.py' -v
"""

import unittest
from unittest import mock

from mininet.container import ( ImageReference, ShellProfile,
                                BashShellProfile, PosixShellProfile )
from mininet.link import Intf, TCIntf, Link, MAX_INTF_NAME_LEN
from mininet.netexec import ( IfconfigTranslator, NamespaceRunner,
                              NetCommandRunner, joinCommand )
from mininet.node import Node
from mininet.util import ( ipAdd, ipNum, ipParse, ipStr, netParse, retry,
                           versionTuple )


class testIfconfigTranslator( unittest.TestCase ):
    """IfconfigTranslator must map legacy ifconfig arguments onto the
       equivalent iproute2 commands, and refuse what it cannot map."""

    def translate( self, *args ):
        """Translate ifconfig arguments for interface 'e0'.

           :param args: ifconfig arguments
           :returns: list of argv lists, or None"""
        return IfconfigTranslator.translate( 'e0', list( args ) )

    def testShow( self ):
        """No arguments shows the interface's addresses."""
        self.assertEqual( self.translate(),
                          [ [ 'ip', 'address', 'show', 'dev', 'e0' ] ] )

    def testUpDown( self ):
        """'up' and 'down' set the link state."""
        for state in ( 'up', 'down' ):
            self.assertEqual( self.translate( state ),
                              [ [ 'ip', 'link', 'set', 'dev', 'e0', state ] ] )

    def testMac( self ):
        """'hw ether MAC' sets the link's MAC address."""
        self.assertEqual(
            self.translate( 'hw', 'ether', 'aa:bb:cc:dd:ee:ff' ),
            [ [ 'ip', 'link', 'set', 'dev', 'e0', 'address',
                'aa:bb:cc:dd:ee:ff' ] ] )

    def testAddressWithNetmaskAndUp( self ):
        """An address with a dotted netmask replaces the old IPv4
           addresses (flush, then add with the prefix length), and the
           state change comes last."""
        self.assertEqual(
            self.translate( '10.0.0.1', 'netmask', '255.255.255.0', 'up' ),
            [ [ 'ip', '-4', 'address', 'flush', 'dev', 'e0' ],
              [ 'ip', 'address', 'add', '10.0.0.1/24', 'dev', 'e0' ],
              [ 'ip', 'link', 'set', 'dev', 'e0', 'up' ] ] )

    def testCidrAddress( self ):
        """An address in ADDR/LEN form keeps its prefix length."""
        self.assertIn( [ 'ip', 'address', 'add', '10.0.0.1/8', 'dev', 'e0' ],
                       self.translate( '10.0.0.1/8' ) )

    def testMtu( self ):
        """'mtu N' sets the link's MTU."""
        self.assertEqual( self.translate( 'mtu', '1400' ),
                          [ [ 'ip', 'link', 'set', 'dev', 'e0', 'mtu',
                              '1400' ] ] )

    def testUnknownFallsBack( self ):
        """Unsupported options, a netmask without an address and an
           incomplete 'hw ether' return None, so the caller runs a real
           ifconfig instead."""
        self.assertIsNone( self.translate( 'promisc' ) )
        self.assertIsNone( self.translate( 'netmask', '255.0.0.0' ) )
        self.assertIsNone( self.translate( 'hw', 'ether' ) )


class testImageReference( unittest.TestCase ):
    """ImageReference.parse() must split image names into repository, tag
       and digest, also when the registry has a port (which contains a
       ':' that is not a tag separator)."""

    #: A syntactically valid sha256 content digest
    DIGEST = 'sha256:' + 'a' * 64

    def testTagged( self ):
        self.assertEqual( ImageReference.parse( 'ubuntu:trusty' ),
                          ImageReference( 'ubuntu', 'trusty' ) )

    def testRegistryPortWithoutTag( self ):
        """The ':5000' of a registry port is not a tag, and an untagged
           image is pulled as 'latest'."""
        ref = ImageReference.parse( 'localhost:5000/img' )
        self.assertEqual( ref, ImageReference( 'localhost:5000/img' ) )
        self.assertEqual( ref.pullTag, 'latest' )

    def testRegistryPortWithTag( self ):
        self.assertEqual( ImageReference.parse( 'localhost:5000/img:v1' ),
                          ImageReference( 'localhost:5000/img', 'v1' ) )

    def testDigest( self ):
        """A digest is split off after '@' and takes precedence over the
           tag when pulling."""
        ref = ImageReference.parse( 'reg:5000/t/img:v1@' + self.DIGEST )
        self.assertEqual( ref, ImageReference( 'reg:5000/t/img', 'v1',
                                               self.DIGEST ) )
        self.assertEqual( ref.pullTag, self.DIGEST )

    def testStr( self ):
        """str() adds the implicit 'latest' tag."""
        self.assertEqual( str( ImageReference.parse( 'frr' ) ), 'frr:latest' )


class testShellProfile( unittest.TestCase ):
    """Control shell profiles (bash and POSIX sh) and the selection of a
       profile for a container. Probing a container is mocked."""

    def testBashDisablesLineEditing( self ):
        """bash runs without readline (no escape sequences in output) and
           carries the 'mininet:NODE' label that 'mn -c' looks for."""
        argv = ShellProfile.forName( 'bash' ).argv( 'mininet:h1' )
        self.assertIn( '--noediting', argv )
        self.assertEqual( argv[ -1 ], 'mininet:h1' )

    def testPosixShellDisablesEnvFile( self ):
        """sh gets the sentinel prompt (chr( 127 )) and an empty ENV, so no
           start-up file runs."""
        env = ShellProfile.forName( 'sh' ).environment( chr( 127 ) )
        self.assertEqual( env, { 'PS1': chr( 127 ), 'ENV': '' } )

    def testBackgroundCommands( self ):
        """Background commands are followed by a printf of the job's PID
           ($!); for POSIX shells the whole text runs in a subshell."""
        bash = ShellProfile.forName( 'bash' ).backgroundCommand( 'x &' )
        posix = ShellProfile.forName( 'sh' ).backgroundCommand( 'x &' )
        self.assertTrue( bash.startswith( 'x & printf' ) )
        self.assertIn( '$!', bash )
        # POSIX shells start jobs from a short-lived subshell
        self.assertTrue( posix.startswith( '( x & printf' ) )
        self.assertTrue( posix.endswith( ')' ) )

    def testUnknownShell( self ):
        with self.assertRaises( ValueError ):
            ShellProfile.forName( 'zsh' )

    def testPreferredSkipsProbing( self ):
        """A shell forced by name is used without probing the container."""
        with mock.patch.object( ShellProfile, 'isAvailableIn' ) as probe:
            profile = ShellProfile.detect( 'mn.x', preferred='sh' )
        self.assertIsInstance( profile, PosixShellProfile )
        probe.assert_not_called()

    def testDetectionPrefersBash( self ):
        """When every shell is available, bash (registered first) wins."""
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                return_value=True ):
            self.assertIsInstance( ShellProfile.detect( 'mn.x' ),
                                   BashShellProfile )

    def testDetectionFallsBackToSh( self ):
        """Without bash, the POSIX sh profile is chosen."""
        def available( profile, _container ):
            return profile.executable == 'sh'
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                autospec=True, side_effect=available ):
            self.assertIsInstance( ShellProfile.detect( 'mn.x' ),
                                   PosixShellProfile )

    def testNoShell( self ):
        """An image without any supported shell is an error."""
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                return_value=False ):
            with self.assertRaises( RuntimeError ):
                ShellProfile.detect( 'mn.x' )


class testNetCommandRunners( unittest.TestCase ):
    """Networking command runners: argument joining, the nsenter command
       line, falling back when nsenter is unusable, and caching of tool
       availability."""

    class RecordingRunner( NetCommandRunner ):
        """Fallback runner that records what it was asked to run and
           reports only ethtool as available."""

        def __init__( self ):
            """Start with an empty list of recorded commands."""
            super().__init__()
            #: Commands passed to run(), as joined strings
            self.calls = []

        def run( self, *args ):
            """Record the command.

               :param args: command and arguments
               :returns: always 'fallback'"""
            self.calls.append( joinCommand( args ) )
            return 'fallback'

        def _probe( self, tool ):
            """Pretend only ethtool exists.

               :param tool: executable name
               :returns: True for 'ethtool'"""
            return tool == 'ethtool'

    def testJoinCommand( self ):
        """Several arguments and a single list both join with spaces."""
        self.assertEqual( joinCommand( ( 'ip link', 'e0' ) ), 'ip link e0' )
        self.assertEqual( joinCommand( ( [ 'a', 'b' ], ) ), 'a b' )

    def testNamespaceRunnerFallsBackWithoutPid( self ):
        """Without a valid PID nsenter is unusable, so commands and tool
           probes go to the fallback runner."""
        fallback = self.RecordingRunner()
        runner = NamespaceRunner( lambda: -1, fallback=fallback )
        self.assertFalse( runner.usable() )
        self.assertEqual( runner.run( 'ip', 'link' ), 'fallback' )
        self.assertEqual( fallback.calls, [ 'ip link' ] )
        self.assertTrue( runner.available( 'ethtool' ) )
        self.assertFalse( runner.available( 'tc' ) )

    def testNamespaceRunnerBuildsNsenterCommand( self ):
        """The command is split into words and run as 'nsenter --target
           PID --net -- COMMAND' (subprocess.run is mocked)."""
        runner = NamespaceRunner( lambda: 4242 )
        with mock.patch.object( NamespaceRunner, 'usable',
                                return_value=True ), \
                mock.patch( 'mininet.netexec.subprocess.run' ) as run:
            run.return_value.stdout = b'ok\n'
            self.assertEqual( runner.run( 'tc qdisc show dev', 'e0' ), 'ok\n' )
        # argv[ 0 ] is the host's nsenter path, which varies
        argv = run.call_args[ 0 ][ 0 ]
        self.assertEqual( argv[ 1: ], [ '--target', '4242', '--net', '--',
                                        'tc', 'qdisc', 'show', 'dev', 'e0' ] )

    def testAvailabilityIsCached( self ):
        """A tool is probed only once per runner."""
        fallback = self.RecordingRunner()
        runner = NamespaceRunner( lambda: -1, fallback=fallback )
        with mock.patch.object( fallback, '_probe',
                                return_value=True ) as probe:
            runner.available( 'tc' )
            runner.available( 'tc' )
        probe.assert_called_once_with( 'tc' )


class testTrafficControlCommands( unittest.TestCase ):
    """TCIntf's generation and validation of tc commands. netem (the
       kernel's network emulator) adds delay, jitter and loss; HTB limits
       bandwidth."""

    def testNetemCommand( self ):
        """Delay, jitter and loss become one netem qdisc with handle 10:,
           and further qdiscs would attach below it (parent 10:1)."""
        cmds, parent = TCIntf.delayCmds( ' root ', delay='10ms', jitter='2ms',
                                         loss=1.5 )
        self.assertEqual( len( cmds ), 1 )
        self.assertIn( 'netem delay 10ms 2ms loss 1.50000', cmds[ 0 ] )
        self.assertEqual( parent, ' parent 10:1 ' )

    def testJitterWithoutDelayIgnored( self ):
        """netem only accepts jitter together with a delay, so jitter alone
           is dropped and no qdisc is added."""
        cmds, parent = TCIntf.delayCmds( ' root ', jitter='2ms' )
        self.assertEqual( cmds, [] )
        self.assertEqual( parent, ' root ' )

    def testZeroLossProducesNoNetem( self ):
        """A loss of 0 alone needs no netem qdisc."""
        self.assertEqual( TCIntf.delayCmds( ' root ', loss=0 )[ 0 ], [] )

    def testBadLossRejected( self ):
        """A loss outside 0 to 100 percent is rejected."""
        self.assertEqual( TCIntf.delayCmds( ' root ', loss=150 )[ 0 ], [] )

    def testZeroBandwidthRejected( self ):
        """A bandwidth of 0 is rejected rather than passed to tc. The
           TCIntf is created without __init__ because no node is needed."""
        intf = TCIntf.__new__( TCIntf )
        self.assertEqual( intf.bwCmds( bw=0 ), ( [], ' root ' ) )

    def testNumberNormalisation( self ):
        """Shaping values given as strings (as JSON from the web GUI
           delivers them) become floats; a trailing '%' is allowed."""
        self.assertEqual( TCIntf._number( '1.5%', 'loss' ), 1.5 )
        self.assertEqual( TCIntf._number( '10', 'bw' ), 10.0 )
        self.assertIsNone( TCIntf._number( None, 'bw' ) )
        with self.assertRaises( ValueError ):
            TCIntf._number( 'fast', 'bw' )

    def testRootQdiscHandle( self ):
        """The root qdisc's handle is read from 'tc qdisc show' output for
           the kernel default (noqueue, handle 0), HTB and netem."""
        regex = TCIntf._rootQdiscRegex
        self.assertEqual( regex.search(
            'qdisc noqueue 0: root refcnt 2' ).group( 1 ), '0' )
        self.assertEqual( regex.search(
            'qdisc htb 5: root refcnt 2 r2q 10' ).group( 1 ), '5' )
        self.assertEqual( regex.search(
            'qdisc netem 10: root refcnt 2 limit 1000' ).group( 1 ), '10' )


class testIpHelpers( unittest.TestCase ):
    """Mininet's IPv4 helpers in mininet.util, which work on addresses as
       unsigned 32-bit integers."""

    def testStrAndNum( self ):
        self.assertEqual( ipStr( 0x0a000001 ), '10.0.0.1' )
        self.assertEqual( ipNum( 192, 168, 1, 254 ), 0xc0a801fe )
        self.assertEqual( ipStr( ipNum( 255, 255, 255, 255 ) ),
                          '255.255.255.255' )

    def testParseShorthand( self ):
        "Short addresses fill in zeros before their last part"
        self.assertEqual( ipStr( ipParse( '10.1' ) ), '10.0.0.1' )
        self.assertEqual( ipStr( ipParse( '10.1.2' ) ), '10.1.0.2' )
        self.assertEqual( ipStr( ipParse( '192.168.1.2' ) ), '192.168.1.2' )

    def testParseRejectsNonsense( self ):
        """Out-of-range octets, too many parts and names are errors."""
        for bad in ( '10.0.0.300', '10.0.0.1.5', 'host' ):
            with self.subTest( bad ), self.assertRaises( ValueError ):
                ipParse( bad )

    def testNetParse( self ):
        """Networks parse with a prefix length, a shortened address, no
           prefix (defaults to /24) or a dotted netmask."""
        self.assertEqual( netParse( '10.0.0.0/8' ), ( 0x0a000000, 8 ) )
        self.assertEqual( netParse( '10.0/8' ), ( 0x0a000000, 8 ) )
        self.assertEqual( netParse( '192.168.1.5' ), ( 0xc0a80105, 24 ) )
        self.assertEqual( netParse( '172.16.0.0/255.240.0.0' ),
                          ( 0xac100000, 12 ) )

    def testAdd( self ):
        """ipAdd() returns the i-th address of a network (10.0.0.0/8 by
           default) and asserts when i does not fit, here in a /30 of four
           addresses."""
        self.assertEqual( ipAdd( 1 ), '10.0.0.1' )
        self.assertEqual( ipAdd( 5, 24, 0xc0a80177 ), '192.168.1.5' )
        self.assertEqual( ipAdd( 3, 30, 0x0aff0004 ), '10.255.0.7' )
        with self.assertRaises( AssertionError ):
            ipAdd( 4, 30, 0x0aff0004 )


class testIpOutputParsing( unittest.TestCase ):
    """Intf's parsing of iproute2 output, and Intf.setIP(), on an
       interface whose node is a mock (no real network)."""

    #: Sample 'ip address show' output for a veth interface 'e0'
    SHOW = ( '2: e0@if3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n'
             '    link/ether 02:42:ac:11:00:02 brd ff:ff:ff:ff:ff:ff\n'
             '    inet 10.0.1.2/30 scope global e0\n' )

    def testAddress( self ):
        """The IPv4 address is read with its prefix length."""
        self.assertEqual( Intf._inetRegex.search( self.SHOW ).group( 1 ),
                          '10.0.1.2/30' )

    @staticmethod
    def intf( show: str = '' ) -> Intf:
        """Create an Intf on a fake node that records commands and answers
           queries.

           The Intf is created without __init__ so no node or link is set
           up; netCmd() returns '' and netRunner.query() returns show.

           :param show: output returned for read-only queries
           :returns: the Intf named 'e0'"""
        intf = Intf.__new__( Intf )
        intf.name, intf.node = 'e0', mock.Mock()
        intf.node.netCmd.return_value = ''
        intf.node.netRunner.query.return_value = show
        return intf

    def testSetIP( self ):
        """setIP() accepts ADDR/LEN, ADDR with a separate prefix length,
           and ADDR/NETMASK, and adds the address with 'ip address add'."""
        for args, expected in ( ( ( '10.0.1.2/30', ), ( '10.0.1.2', 30 ) ),
                                ( ( '10.0.1.2', 30 ), ( '10.0.1.2', 30 ) ),
                                ( ( '10.0.1.2/255.255.255.0', ),
                                  ( '10.0.1.2', 24 ) ) ):
            with self.subTest( args ):
                intf = self.intf()
                intf.setIP( *args )
                self.assertEqual( ( intf.ip, intf.prefixLen ), expected )
                intf.node.netCmd.assert_called_with(
                    'ip', 'address', 'add', '%s/%d' % expected, 'dev', 'e0' )

    def testSetIPRejectsMistakes( self ):
        """A missing prefix length and an invalid address raise
           ValueError instead of configuring something wrong."""
        with self.assertRaises( ValueError ):
            self.intf().setIP( '10.0.1.2' )              # no prefix
        with self.assertRaises( ValueError ):
            self.intf().setIP( '10.0.1.300/24' )

    def testUpdateAddr( self ):
        """updateAddr() reads the IP, MAC and prefix length from the
           query output, and returns ( None, None ) when there are none."""
        intf = self.intf( self.SHOW )
        self.assertEqual( intf.updateAddr(), ( '10.0.1.2', '02:42:ac:11:00:02' ) )
        self.assertEqual( intf.prefixLen, 30 )
        self.assertEqual( self.intf( 'no addresses' ).updateAddr(), ( None, None ) )

    def testMac( self ):
        self.assertEqual( Intf._etherRegex.search( self.SHOW ).group( 1 ),
                          '02:42:ac:11:00:02' )

    def testFlags( self ):
        """The UP flag is detected as a whole word in the <...> flag
           list."""
        up = Intf._flagsRegex.search( self.SHOW ).group( 1 ).split( ',' )
        down = Intf._flagsRegex.search(
            '<BROADCAST,MULTICAST> state DOWN' ).group( 1 ).split( ',' )
        self.assertIn( 'UP', up )
        self.assertNotIn( 'UP', down )   # LOWER_UP must not count as UP

    def testPromptSentinel( self ):
        """Node._promptRegex finds the sentinel prompt chr( 127 ) only at
           the end of output, optionally followed by terminal escape
           sequences (BusyBox ash sends the cursor position query
           ESC [6n after its prompt)."""
        regex = Node._promptRegex
        self.assertTrue( regex.search( chr( 127 ) ) )
        self.assertTrue( regex.search( 'motd' + chr( 127 ) + '\x1b[6n' ) )
        self.assertFalse( regex.search( chr( 127 ) + 'text' ) )


class testUtilities( unittest.TestCase ):
    """Utility helpers from mininet.util and mininet.link."""

    def testVersionTuple( self ):
        """Versions compare numerically, not as strings."""
        self.assertLess( versionTuple( '2.17.9' ), versionTuple( '2.17.10' ) )
        self.assertLess( versionTuple( '1.9' ), ( 1, 10 ) )

    def testRetrySucceedsOnLastAttempt( self ):
        """retry( 3, ... ) makes one attempt plus three retries, so a
           function that succeeds on its fourth call is accepted."""
        calls = []

        def succeedOnFourthCall():
            calls.append( 1 )
            return len( calls ) == 4

        retry( 3, 0, succeedOnFourthCall )   # must not exit
        self.assertEqual( len( calls ), 4 )

    def testRetryGivesUp( self ):
        """retry() exits the program when every attempt fails."""
        with self.assertRaises( SystemExit ):
            retry( 2, 0, lambda: False )

    def testInterfaceNameLimit( self ):
        """Link.intfName() rejects names longer than the kernel allows
           (MAX_INTF_NAME_LEN, 15 characters) instead of letting them be
           truncated."""
        node = mock.Mock()
        # One character too long once '-eth0' is appended
        node.name = 'x' * ( MAX_INTF_NAME_LEN - len( '-eth0' ) + 1 )
        with self.assertRaises( ValueError ):
            Link.intfName( Link.__new__( Link ), node, 0 )
        node.name = 'wan_r'
        self.assertEqual( Link.intfName( Link.__new__( Link ), node, 0 ),
                          'wan_r-eth0' )


if __name__ == '__main__':
    unittest.main()
