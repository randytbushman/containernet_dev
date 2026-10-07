"""
Unit tests for container compatibility logic. Needs neither Docker nor root.

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
from mininet.util import retry, versionTuple


class testIfconfigTranslator( unittest.TestCase ):
    """Legacy ifconfig arguments must map onto iproute2."""

    def translate( self, *args ):
        """Translate args for interface 'e0'."""
        return IfconfigTranslator.translate( 'e0', list( args ) )

    def testShow( self ):
        self.assertEqual( self.translate(),
                          [ [ 'ip', 'address', 'show', 'dev', 'e0' ] ] )

    def testUpDown( self ):
        for state in ( 'up', 'down' ):
            self.assertEqual( self.translate( state ),
                              [ [ 'ip', 'link', 'set', 'dev', 'e0', state ] ] )

    def testMac( self ):
        self.assertEqual(
            self.translate( 'hw', 'ether', 'aa:bb:cc:dd:ee:ff' ),
            [ [ 'ip', 'link', 'set', 'dev', 'e0', 'address',
                'aa:bb:cc:dd:ee:ff' ] ] )

    def testAddressWithNetmaskAndUp( self ):
        self.assertEqual(
            self.translate( '10.0.0.1', 'netmask', '255.255.255.0', 'up' ),
            [ [ 'ip', '-4', 'address', 'flush', 'dev', 'e0' ],
              [ 'ip', 'address', 'add', '10.0.0.1/24', 'dev', 'e0' ],
              [ 'ip', 'link', 'set', 'dev', 'e0', 'up' ] ] )

    def testCidrAddress( self ):
        self.assertIn( [ 'ip', 'address', 'add', '10.0.0.1/8', 'dev', 'e0' ],
                       self.translate( '10.0.0.1/8' ) )

    def testMtu( self ):
        self.assertEqual( self.translate( 'mtu', '1400' ),
                          [ [ 'ip', 'link', 'set', 'dev', 'e0', 'mtu',
                              '1400' ] ] )

    def testUnknownFallsBack( self ):
        self.assertIsNone( self.translate( 'promisc' ) )
        self.assertIsNone( self.translate( 'netmask', '255.0.0.0' ) )
        self.assertIsNone( self.translate( 'hw', 'ether' ) )


class testImageReference( unittest.TestCase ):
    """Image names must be split into repository, tag and digest."""

    DIGEST = 'sha256:' + 'a' * 64

    def testTagged( self ):
        self.assertEqual( ImageReference.parse( 'ubuntu:trusty' ),
                          ImageReference( 'ubuntu', 'trusty' ) )

    def testRegistryPortWithoutTag( self ):
        ref = ImageReference.parse( 'localhost:5000/img' )
        self.assertEqual( ref, ImageReference( 'localhost:5000/img' ) )
        self.assertEqual( ref.pullTag, 'latest' )

    def testRegistryPortWithTag( self ):
        self.assertEqual( ImageReference.parse( 'localhost:5000/img:v1' ),
                          ImageReference( 'localhost:5000/img', 'v1' ) )

    def testDigest( self ):
        ref = ImageReference.parse( 'reg:5000/t/img:v1@' + self.DIGEST )
        self.assertEqual( ref, ImageReference( 'reg:5000/t/img', 'v1',
                                               self.DIGEST ) )
        self.assertEqual( ref.pullTag, self.DIGEST )

    def testStr( self ):
        self.assertEqual( str( ImageReference.parse( 'frr' ) ), 'frr:latest' )


class testShellProfile( unittest.TestCase ):
    """Shell profiles and their selection."""

    def testBashDisablesLineEditing( self ):
        argv = ShellProfile.forName( 'bash' ).argv( 'mininet:h1' )
        self.assertIn( '--noediting', argv )
        self.assertEqual( argv[ -1 ], 'mininet:h1' )

    def testPosixShellDisablesEnvFile( self ):
        env = ShellProfile.forName( 'sh' ).environment( chr( 127 ) )
        self.assertEqual( env, { 'PS1': chr( 127 ), 'ENV': '' } )

    def testBackgroundCommands( self ):
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
        with mock.patch.object( ShellProfile, 'isAvailableIn' ) as probe:
            profile = ShellProfile.detect( 'mn.x', preferred='sh' )
        self.assertIsInstance( profile, PosixShellProfile )
        probe.assert_not_called()

    def testDetectionPrefersBash( self ):
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                return_value=True ):
            self.assertIsInstance( ShellProfile.detect( 'mn.x' ),
                                   BashShellProfile )

    def testDetectionFallsBackToSh( self ):
        def available( profile, _container ):
            return profile.executable == 'sh'
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                autospec=True, side_effect=available ):
            self.assertIsInstance( ShellProfile.detect( 'mn.x' ),
                                   PosixShellProfile )

    def testNoShell( self ):
        with mock.patch.object( ShellProfile, 'isAvailableIn',
                                return_value=False ):
            with self.assertRaises( RuntimeError ):
                ShellProfile.detect( 'mn.x' )


class testNetCommandRunners( unittest.TestCase ):
    """Runner strategy selection and fallbacks."""

    class RecordingRunner( NetCommandRunner ):
        """Fallback runner that records what it was asked to run."""

        def __init__( self ):
            super().__init__()
            self.calls = []

        def run( self, *args ):
            self.calls.append( joinCommand( args ) )
            return 'fallback'

        def _probe( self, tool ):
            return tool == 'ethtool'

    def testJoinCommand( self ):
        self.assertEqual( joinCommand( ( 'ip link', 'e0' ) ), 'ip link e0' )
        self.assertEqual( joinCommand( ( [ 'a', 'b' ], ) ), 'a b' )

    def testNamespaceRunnerFallsBackWithoutPid( self ):
        fallback = self.RecordingRunner()
        runner = NamespaceRunner( lambda: -1, fallback=fallback )
        self.assertFalse( runner.usable() )
        self.assertEqual( runner.run( 'ip', 'link' ), 'fallback' )
        self.assertEqual( fallback.calls, [ 'ip link' ] )
        self.assertTrue( runner.available( 'ethtool' ) )
        self.assertFalse( runner.available( 'tc' ) )

    def testNamespaceRunnerBuildsNsenterCommand( self ):
        runner = NamespaceRunner( lambda: 4242 )
        with mock.patch.object( NamespaceRunner, 'usable',
                                return_value=True ), \
                mock.patch( 'mininet.netexec.subprocess.run' ) as run:
            run.return_value.stdout = b'ok\n'
            self.assertEqual( runner.run( 'tc qdisc show dev', 'e0' ), 'ok\n' )
        argv = run.call_args[ 0 ][ 0 ]
        self.assertEqual( argv[ 1: ], [ '--target', '4242', '--net', '--',
                                        'tc', 'qdisc', 'show', 'dev', 'e0' ] )

    def testAvailabilityIsCached( self ):
        fallback = self.RecordingRunner()
        runner = NamespaceRunner( lambda: -1, fallback=fallback )
        with mock.patch.object( fallback, '_probe',
                                return_value=True ) as probe:
            runner.available( 'tc' )
            runner.available( 'tc' )
        probe.assert_called_once_with( 'tc' )


class testTrafficControlCommands( unittest.TestCase ):
    """tc command generation and validation."""

    def testNetemCommand( self ):
        cmds, parent = TCIntf.delayCmds( ' root ', delay='10ms', jitter='2ms',
                                         loss=1.5 )
        self.assertEqual( len( cmds ), 1 )
        self.assertIn( 'netem delay 10ms 2ms loss 1.50000', cmds[ 0 ] )
        self.assertEqual( parent, ' parent 10:1 ' )

    def testJitterWithoutDelayIgnored( self ):
        cmds, parent = TCIntf.delayCmds( ' root ', jitter='2ms' )
        self.assertEqual( cmds, [] )
        self.assertEqual( parent, ' root ' )

    def testZeroLossProducesNoNetem( self ):
        self.assertEqual( TCIntf.delayCmds( ' root ', loss=0 )[ 0 ], [] )

    def testBadLossRejected( self ):
        self.assertEqual( TCIntf.delayCmds( ' root ', loss=150 )[ 0 ], [] )

    def testZeroBandwidthRejected( self ):
        intf = TCIntf.__new__( TCIntf )
        self.assertEqual( intf.bwCmds( bw=0 ), ( [], ' root ' ) )

    def testNumberNormalisation( self ):
        self.assertEqual( TCIntf._number( '1.5%', 'loss' ), 1.5 )
        self.assertEqual( TCIntf._number( '10', 'bw' ), 10.0 )
        self.assertIsNone( TCIntf._number( None, 'bw' ) )
        with self.assertRaises( ValueError ):
            TCIntf._number( 'fast', 'bw' )

    def testRootQdiscHandle( self ):
        regex = TCIntf._rootQdiscRegex
        self.assertEqual( regex.search(
            'qdisc noqueue 0: root refcnt 2' ).group( 1 ), '0' )
        self.assertEqual( regex.search(
            'qdisc htb 5: root refcnt 2 r2q 10' ).group( 1 ), '5' )
        self.assertEqual( regex.search(
            'qdisc netem 10: root refcnt 2 limit 1000' ).group( 1 ), '10' )


class testIpOutputParsing( unittest.TestCase ):
    """Parsing of iproute2 output used by Intf."""

    SHOW = ( '2: e0@if3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n'
             '    link/ether 02:42:ac:11:00:02 brd ff:ff:ff:ff:ff:ff\n'
             '    inet 10.0.1.2/30 scope global e0\n' )

    def testAddress( self ):
        self.assertEqual( Intf._inetRegex.search( self.SHOW ).groups(),
                          ( '10.0.1.2', '30' ) )

    def testMac( self ):
        self.assertEqual( Intf._etherRegex.search( self.SHOW ).group( 1 ),
                          '02:42:ac:11:00:02' )

    def testFlags( self ):
        up = Intf._flagsRegex.search( self.SHOW ).group( 1 ).split( ',' )
        down = Intf._flagsRegex.search(
            '<BROADCAST,MULTICAST> state DOWN' ).group( 1 ).split( ',' )
        self.assertIn( 'UP', up )
        self.assertNotIn( 'UP', down )   # LOWER_UP must not count as UP

    def testPromptSentinel( self ):
        regex = Node._promptRegex
        self.assertTrue( regex.search( chr( 127 ) ) )
        self.assertTrue( regex.search( 'motd' + chr( 127 ) + '\x1b[6n' ) )
        self.assertFalse( regex.search( chr( 127 ) + 'text' ) )


class testUtilities( unittest.TestCase ):
    """Utility helpers."""

    def testVersionTuple( self ):
        self.assertLess( versionTuple( '2.17.9' ), versionTuple( '2.17.10' ) )
        self.assertLess( versionTuple( '1.9' ), ( 1, 10 ) )

    def testRetrySucceedsOnLastAttempt( self ):
        calls = []

        def succeedOnFourthCall():
            calls.append( 1 )
            return len( calls ) == 4

        retry( 3, 0, succeedOnFourthCall )   # must not exit
        self.assertEqual( len( calls ), 4 )

    def testRetryGivesUp( self ):
        with self.assertRaises( SystemExit ):
            retry( 2, 0, lambda: False )

    def testInterfaceNameLimit( self ):
        node = mock.Mock()
        node.name = 'x' * ( MAX_INTF_NAME_LEN - len( '-eth0' ) + 1 )
        with self.assertRaises( ValueError ):
            Link.intfName( Link.__new__( Link ), node, 0 )
        node.name = 'wan_r'
        self.assertEqual( Link.intfName( Link.__new__( Link ), node, 0 ),
                          'wan_r-eth0' )


if __name__ == '__main__':
    unittest.main()
