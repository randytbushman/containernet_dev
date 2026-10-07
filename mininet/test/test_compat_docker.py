"""
Docker node tests across image families. Needs Docker access, not root.

Without root, network commands fall back to the container's own shell,
so interface tests only run on images that ship iproute2/BusyBox ip.

    python3 -m unittest discover -s mininet/test -p 'test_compat_docker.py' -v
"""

import os
import signal
import subprocess
import threading
import unittest

from compat_support import ( DockerTestCase, BASH_IPROUTE_IMAGE,
                             BASH_MINIMAL_IMAGE, BUSYBOX_IMAGE,
                             containerExists, isRoot )
from mininet.link import Intf, TCIntf
from mininet.log import setLogLevel
from mininet.node import Docker


def noMove( *_args, **_kwargs ):
    """moveIntfFn that keeps an existing container interface in place."""
    return None


class DockerImageMixin( object ):
    """Tests run once per image family; subclasses set the image."""

    image = None
    expectedShell = None
    hasIpTool = True

    def setUp( self ):
        """Start one container for the test."""
        setLogLevel( 'warning' )
        self.node = Docker( 'cnt1', dimage=self.image, rm=True )

    def tearDown( self ):
        """Remove the container."""
        self.node.terminate()
        super().tearDown()

    def testShellDetected( self ):
        self.assertEqual( self.node.shellProfile.executable,
                          self.expectedShell )

    def testCmdOutputHasNoControlSequences( self ):
        self.assertEqual( self.node.cmd( 'echo hello' ), 'hello\r\n' )
        out = self.node.cmd( 'cat /etc/hostname; echo a; echo b' )
        self.assertNotIn( '\x1b', out )
        self.assertEqual( out, 'cnt1\r\na\r\nb\r\n' )

    def testExitStatusAndMultiline( self ):
        self.assertEqual( self.node.cmd( 'false; echo $?' ), '1\r\n' )

    def testPexecHasNoTty( self ):
        out, err, code = self.node.pexec( 'echo hi' )
        self.assertEqual( ( out, err, code ), ( 'hi\n', '', 0 ) )

    def testPopenShellUsesContainerShell( self ):
        proc = self.node.popen( 'echo $((1+2))', shell=True )
        out, _err = proc.communicate()
        self.assertEqual( out, b'3\n' )

    def testPrintPidWithoutMnexec( self ):
        self.node.sendCmd( 'sleep 0.2', printPid=True )
        self.node.waitOutput()
        self.assertIsInstance( self.node.lastPid, int )
        self.assertGreater( self.node.lastPid, 0 )

    def testBackgroundJobsKeepOutputAligned( self ):
        # A job finishing while the shell idles must not emit an extra
        # sentinel (BusyBox ash re-prints its prompt on SIGCHLD)
        for i in range( 5 ):
            self.node.cmd( 'sleep 0.05 &' )
            self.assertGreater( self.node.lastPid or 0, 0 )
            self.node.cmd( 'sleep 0.2' )
            self.assertEqual( self.node.cmd( 'echo %d' % i ), '%d\r\n' % i )

    def testLoneShellCmdIsNotRerun( self ):
        self.assertIsNone( self.node.imageStartCommand() )

    def testConcurrentCmd( self ):
        outputs = {}

        def worker( i ):
            outputs[ i ] = self.node.cmd( 'echo %d' % i )

        threads = [ threading.Thread( target=worker, args=( i, ) )
                    for i in range( 8 ) ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual( outputs, { i: '%d\r\n' % i for i in range( 8 ) } )

    def testShellRestartsAfterDying( self ):
        os.kill( self.node.shell.pid, signal.SIGKILL )
        self.node.shell.wait()
        self.assertEqual( self.node.cmd( 'echo back' ), 'back\r\n' )

    def testInterfaceState( self ):
        if not ( self.hasIpTool or isRoot() ):
            self.skipTest( 'image has no ip tool and nsenter needs root' )
        intf = Intf( 'eth0', node=self.node, moveIntfFn=noMove )
        self.assertEqual( intf.get_state(), 'up' )
        intf.updateAddr()
        self.assertIsNotNone( intf.IP() )
        self.assertIsNotNone( intf.MAC() )
        self.assertEqual( intf.status(), 'OK' )
        intf.set_state( 'down' )
        self.assertEqual( intf.get_state(), 'down' )
        intf.config()   # reconfiguring must not force the interface up
        self.assertEqual( intf.get_state(), 'down' )
        intf.set_state( 'up' )
        self.assertEqual( intf.get_state(), 'up' )
        with self.assertRaises( ValueError ):
            intf.set_state( 'sideways' )
        self.node.delIntf( intf )


class testUbuntuTrusty( DockerImageMixin, DockerTestCase ):
    """bash + iproute2 (the image used by NetworkDegradationSimulation)."""

    image = BASH_IPROUTE_IMAGE
    expectedShell = 'bash'

    def testTrafficShapingAppliesAndResets( self ):
        intf = TCIntf( 'eth0', node=self.node, moveIntfFn=noMove )
        intf.config( delay='10ms', loss=2 )
        qdisc = self.node.cmd( 'tc qdisc show dev eth0' )
        self.assertIn( 'netem', qdisc )
        self.assertIn( 'delay 10', qdisc )
        intf.config( loss=0 )   # nothing requested -> default qdisc
        self.assertNotIn( 'netem', self.node.cmd( 'tc qdisc show dev eth0' ) )
        self.node.delIntf( intf )


class testDebianSlim( DockerImageMixin, DockerTestCase ):
    """bash without iproute2/net-tools/ethtool."""

    image = BASH_MINIMAL_IMAGE
    expectedShell = 'bash'
    hasIpTool = False


class testAlpine( DockerImageMixin, DockerTestCase ):
    """BusyBox sh only (no bash)."""

    image = BUSYBOX_IMAGE
    expectedShell = 'sh'


class testDockerLifecycle( DockerTestCase ):
    """Container creation options and removal."""

    def setUp( self ):
        setLogLevel( 'warning' )

    def testPrivilegedIsHonoured( self ):
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE, privileged=True )
        config = node.dcli.inspect_container( node.did )[ 'HostConfig' ]
        node.terminate()
        self.assertTrue( config[ 'Privileged' ] )
        self.assertIn( 'NET_ADMIN', [ c.upper() for c in config[ 'CapAdd' ] ] )

    def testCallerListsAreNotMutated( self ):
        capAdd = [ 'sys_admin' ]
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE, cap_add=capAdd )
        node.terminate()
        self.assertEqual( capAdd, [ 'sys_admin' ] )

    def testDefaultsAreNotShared( self ):
        first = Docker( 'cnt1', dimage=BUSYBOX_IMAGE )
        second = Docker( 'cnt2', dimage=BUSYBOX_IMAGE )
        first.environment[ 'ONLY_FIRST' ] = '1'
        sharedVolumes = first.volumes is second.volumes
        first.terminate()
        second.terminate()
        self.assertNotIn( 'ONLY_FIRST', second.environment )
        self.assertFalse( sharedVolumes )

    def testTerminateRemovesStoppedContainer( self ):
        node = Docker( 'cnt2', dimage=BUSYBOX_IMAGE )
        subprocess.run( [ 'docker', 'stop', '-t', '0', 'mn.cnt2' ],
                        capture_output=True, check=True )
        node.terminate()
        self.assertFalse( containerExists( 'mn.cnt2' ) )

    def testRmMatchesExactName( self ):
        subprocess.run( [ 'docker', 'run', '-d', '--name', 'mn.cnt30',
                          '--label', 'com.containernet',
                          BUSYBOX_IMAGE, 'sleep', '60' ],
                        capture_output=True, check=True )
        node = Docker( 'cnt3', dimage=BUSYBOX_IMAGE, rm=True )
        node.terminate()
        self.assertTrue( containerExists( 'mn.cnt30' ) )

    def testRmReplacesStaleContainer( self ):
        subprocess.run( [ 'docker', 'run', '-d', '--name', 'mn.cnt4',
                          '--label', 'com.containernet',
                          BUSYBOX_IMAGE, 'sleep', '60' ],
                        capture_output=True, check=True )
        node = Docker( 'cnt4', dimage=BUSYBOX_IMAGE, rm=True )
        self.assertEqual( node.cmd( 'echo ok' ), 'ok\r\n' )
        node.terminate()

    def testForcedShell( self ):
        node = Docker( 'cnt1', dimage=BASH_IPROUTE_IMAGE, dshell='sh' )
        shell = node.shellProfile.executable
        out = node.cmd( 'echo forced' )
        node.terminate()
        self.assertEqual( ( shell, out ), ( 'sh', 'forced\r\n' ) )

    def testImageEntrypointCommandIsQuoted( self ):
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE )
        node._imageConfigCache[ BUSYBOX_IMAGE ] = {
            'Entrypoint': [ '/bin/sh', '-c' ],
            'Cmd': [ 'echo "a b" > /tmp/started' ] }
        command = node.imageStartCommand()
        node.start()
        node.cmd( 'sleep 0.5' )
        started = node.cmd( 'cat /tmp/started' )
        node.terminate()
        self.assertEqual( command,
                          '/bin/sh -c \'echo "a b" > /tmp/started\'' )
        self.assertEqual( started, 'a b\r\n' )

    def testResourceUpdates( self ):
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE )
        self.assertTrue( node.updateCpuLimit( cpu_shares=512 ) )
        self.assertTrue( node.updateCpuLimit( cpu_period=50001,
                                              cpu_quota=20000 ) )
        self.assertTrue( node.updateMemoryLimit( mem_limit=66093056 ) )
        resources = dict( node.resources )
        hostConfig = node.dcli.inspect_container( node.did )[ 'HostConfig' ]
        node.terminate()
        self.assertEqual( resources[ 'cpu_shares' ], 512 )
        self.assertEqual( resources[ 'cpu_quota' ], 20000 )
        self.assertEqual( hostConfig[ 'CpuPeriod' ], 50001 )
        self.assertEqual( hostConfig[ 'Memory' ], 66093056 )

    def testMissingImageRaises( self ):
        with self.assertRaises( RuntimeError ):
            Docker( 'cnt1', dimage='localhost:5999/does-not-exist:v1' )
        self.assertFalse( containerExists( 'mn.cnt1' ) )


if __name__ == '__main__':
    unittest.main()
