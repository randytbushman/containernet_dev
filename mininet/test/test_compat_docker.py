"""
Docker node tests across image families. Needs Docker access, not root.

These tests start single Containernet Docker nodes (mininet.node.Docker)
without building a network, once for each image family in compat_support
(bash with iproute2, bash without networking tools, BusyBox sh only). They
check that the control shell is detected and driven correctly (clean
command output, exit status, background jobs, PID reporting, concurrent
use, restart after the shell dies), that pexec() and popen() work, that an
existing interface can be inspected and switched up and down, and that
container options, image start commands, resource updates and container
removal behave as documented.

Without root, nsenter cannot be used, so network commands fall back to the
container's own shell. Interface tests therefore only run on images that
ship iproute2 or BusyBox ip unless the tests run as root.

Run from the containernet directory with::

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
    """moveIntfFn that keeps an existing container interface in place.

       Intf() normally moves a new interface into its node's namespace;
       the tests wrap eth0, which Docker already created inside the
       container, so there is nothing to move.

       :returns: None"""
    return None


class DockerImageMixin( object ):
    """Tests that run once per image family; subclasses set the image.

       Each test gets a fresh container named 'mn.cnt1'. The mixin comes
       before DockerTestCase in the subclasses' bases so that its setUp()
       and tearDown() are the ones used; tearDown() then calls
       DockerTestCase.tearDown() to remove any remaining containers."""

    #: Image reference to start
    image = None
    #: Executable of the shell profile that detection should choose
    expectedShell = None
    #: Whether the image ships an ip command (needed without root)
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
        """The control shell matches what the image provides."""
        self.assertEqual( self.node.shellProfile.executable,
                          self.expectedShell )

    def testCmdOutputHasNoControlSequences( self ):
        """cmd() returns exactly the command's output: no echo, prompt or
           terminal escape sequences. Lines end in CR LF because the shell
           runs on a pseudo-terminal."""
        self.assertEqual( self.node.cmd( 'echo hello' ), 'hello\r\n' )
        out = self.node.cmd( 'cat /etc/hostname; echo a; echo b' )
        self.assertNotIn( '\x1b', out )
        self.assertEqual( out, 'cnt1\r\na\r\nb\r\n' )

    def testExitStatusAndMultiline( self ):
        """$? reflects the previous command run through the same shell."""
        self.assertEqual( self.node.cmd( 'false; echo $?' ), '1\r\n' )

    def testPexecHasNoTty( self ):
        """pexec() runs without a terminal: plain LF line endings and
           separate stdout and stderr."""
        out, err, code = self.node.pexec( 'echo hi' )
        self.assertEqual( ( out, err, code ), ( 'hi\n', '', 0 ) )

    def testPopenShellUsesContainerShell( self ):
        """popen( ..., shell=True ) interprets shell syntax inside the
           container, not with the host's shell."""
        proc = self.node.popen( 'echo $((1+2))', shell=True )
        out, _err = proc.communicate()
        self.assertEqual( out, b'3\n' )

    def testPrintPidWithoutMnexec( self ):
        """printPid works although mnexec (Mininet's helper that prints a
           command's PID) does not exist in containers."""
        self.node.sendCmd( 'sleep 0.2', printPid=True )
        self.node.waitOutput()
        self.assertIsInstance( self.node.lastPid, int )
        self.assertGreater( self.node.lastPid, 0 )

    def testBackgroundJobsKeepOutputAligned( self ):
        """Background jobs report their PID and do not disturb the output
           of later commands."""
        # A job finishing while the shell idles must not emit an extra
        # sentinel (BusyBox ash re-prints its prompt on SIGCHLD)
        for i in range( 5 ):
            self.node.cmd( 'sleep 0.05 &' )
            self.assertGreater( self.node.lastPid or 0, 0 )
            self.node.cmd( 'sleep 0.2' )
            self.assertEqual( self.node.cmd( 'echo %d' % i ), '%d\r\n' % i )

    def testLoneShellCmdIsNotRerun( self ):
        """An image CMD that only starts a shell (as in all test images)
           is not run again, since Containernet provides its own shell."""
        self.assertIsNone( self.node.imageStartCommand() )

    def testConcurrentCmd( self ):
        """cmd() calls from several threads are serialised, and each
           caller gets its own command's output."""
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
        """When the control shell is killed, the next cmd() starts a new
           one while the container is still running."""
        os.kill( self.node.shell.pid, signal.SIGKILL )
        self.node.shell.wait()
        self.assertEqual( self.node.cmd( 'echo back' ), 'back\r\n' )

    def testInterfaceState( self ):
        """Docker's own eth0 can be inspected (state, IP, MAC, status) and
           switched down and up; reconfiguring a down interface leaves it
           down, and an invalid state is rejected."""
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
    """bash with iproute2 (the host image used by
       NetworkDegradationSimulation)."""

    image = BASH_IPROUTE_IMAGE
    expectedShell = 'bash'

    def testTrafficShapingAppliesAndResets( self ):
        """TCIntf.config() installs a netem qdisc (delay and loss), and a
           later config() with nothing to shape removes it again. This
           image ships tc, so the result can be checked from inside."""
        intf = TCIntf( 'eth0', node=self.node, moveIntfFn=noMove )
        intf.config( delay='10ms', loss=2 )
        qdisc = self.node.cmd( 'tc qdisc show dev eth0' )
        self.assertIn( 'netem', qdisc )
        self.assertIn( 'delay 10', qdisc )
        # Nothing to shape, so the kernel's default qdisc returns
        intf.config( loss=0 )
        self.assertNotIn( 'netem', self.node.cmd( 'tc qdisc show dev eth0' ) )
        self.node.delIntf( intf )


class testDebianSlim( DockerImageMixin, DockerTestCase ):
    """bash without iproute2, net-tools or ethtool."""

    image = BASH_MINIMAL_IMAGE
    expectedShell = 'bash'
    hasIpTool = False


class testAlpine( DockerImageMixin, DockerTestCase ):
    """BusyBox sh only (no bash)."""

    image = BUSYBOX_IMAGE
    expectedShell = 'sh'


class testDockerLifecycle( DockerTestCase ):
    """Container creation options, image start commands, resource updates
       and removal. Each test creates and removes its own containers."""

    def setUp( self ):
        """Keep Containernet's log output quiet."""
        setLogLevel( 'warning' )

    def testPrivilegedIsHonoured( self ):
        """privileged=True reaches Docker, and the NET_ADMIN capability
           that Containernet always adds is still present."""
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE, privileged=True )
        config = node.dcli.inspect_container( node.did )[ 'HostConfig' ]
        node.terminate()
        self.assertTrue( config[ 'Privileged' ] )
        self.assertIn( 'NET_ADMIN', [ c.upper() for c in config[ 'CapAdd' ] ] )

    def testCallerListsAreNotMutated( self ):
        """Adding NET_ADMIN must not modify the caller's cap_add list."""
        capAdd = [ 'sys_admin' ]
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE, cap_add=capAdd )
        node.terminate()
        self.assertEqual( capAdd, [ 'sys_admin' ] )

    def testDefaultsAreNotShared( self ):
        """Two nodes never share the mutable default environment or volume
           list."""
        first = Docker( 'cnt1', dimage=BUSYBOX_IMAGE )
        second = Docker( 'cnt2', dimage=BUSYBOX_IMAGE )
        first.environment[ 'ONLY_FIRST' ] = '1'
        sharedVolumes = first.volumes is second.volumes
        first.terminate()
        second.terminate()
        self.assertNotIn( 'ONLY_FIRST', second.environment )
        self.assertFalse( sharedVolumes )

    def testTerminateRemovesStoppedContainer( self ):
        """terminate() also removes a container that has already
           stopped."""
        node = Docker( 'cnt2', dimage=BUSYBOX_IMAGE )
        subprocess.run( [ 'docker', 'stop', '-t', '0', 'mn.cnt2' ],
                        capture_output=True, check=True )
        node.terminate()
        self.assertFalse( containerExists( 'mn.cnt2' ) )

    def testRmMatchesExactName( self ):
        """rm=True removes a left-over container with exactly the node's
           name only: 'mn.cnt30' survives creating and removing node
           'cnt3' (container 'mn.cnt3')."""
        subprocess.run( [ 'docker', 'run', '-d', '--name', 'mn.cnt30',
                          '--label', 'com.containernet',
                          BUSYBOX_IMAGE, 'sleep', '60' ],
                        capture_output=True, check=True )
        node = Docker( 'cnt3', dimage=BUSYBOX_IMAGE, rm=True )
        node.terminate()
        self.assertTrue( containerExists( 'mn.cnt30' ) )

    def testRmReplacesStaleContainer( self ):
        """rm=True replaces a left-over container of the same name instead
           of failing on the name conflict."""
        subprocess.run( [ 'docker', 'run', '-d', '--name', 'mn.cnt4',
                          '--label', 'com.containernet',
                          BUSYBOX_IMAGE, 'sleep', '60' ],
                        capture_output=True, check=True )
        node = Docker( 'cnt4', dimage=BUSYBOX_IMAGE, rm=True )
        self.assertEqual( node.cmd( 'echo ok' ), 'ok\r\n' )
        node.terminate()

    def testForcedShell( self ):
        """dshell='sh' uses the POSIX shell profile even on a bash
           image."""
        node = Docker( 'cnt1', dimage=BASH_IPROUTE_IMAGE, dshell='sh' )
        shell = node.shellProfile.executable
        out = node.cmd( 'echo forced' )
        node.terminate()
        self.assertEqual( ( shell, out ), ( 'sh', 'forced\r\n' ) )

    def testImageEntrypointCommandIsQuoted( self ):
        """The image's ENTRYPOINT and CMD are joined with shell quoting,
           so an argument containing spaces and quotes reaches the
           entrypoint intact when start() runs it. The image configuration
           is faked through the node's cache."""
        node = Docker( 'cnt1', dimage=BUSYBOX_IMAGE )
        node._imageConfigCache[ BUSYBOX_IMAGE ] = {
            'Entrypoint': [ '/bin/sh', '-c' ],
            'Cmd': [ 'echo "a b" > /tmp/started' ] }
        command = node.imageStartCommand()
        node.start()
        # start() runs the command in the background; give it time
        node.cmd( 'sleep 0.5' )
        started = node.cmd( 'cat /tmp/started' )
        node.terminate()
        self.assertEqual( command,
                          '/bin/sh -c \'echo "a b" > /tmp/started\'' )
        self.assertEqual( started, 'a b\r\n' )

    def testResourceUpdates( self ):
        """CPU and memory limits can be changed on a running container;
           the node's resources and Docker's host configuration both
           reflect the new values (memory in bytes)."""
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
        """An image that can be neither found nor pulled raises
           RuntimeError and leaves no container behind. The reference
           names a registry on localhost:5999 that is not expected to
           exist."""
        with self.assertRaises( RuntimeError ):
            Docker( 'cnt1', dimage='localhost:5999/does-not-exist:v1' )
        self.assertFalse( containerExists( 'mn.cnt1' ) )


if __name__ == '__main__':
    unittest.main()
