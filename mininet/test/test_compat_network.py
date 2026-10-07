"""
End-to-end topology tests across image families. Needs root and Docker.

Covers veth links into containers, nsenter-based configuration of images
without networking tools, link-level addressing, routing with failover,
tc shaping/reset and interface up/down. The routed test mirrors the
NetworkDegradationSimulation "simple redundancy" experiment.

    sudo python3 -m unittest discover -s mininet/test -p 'test_compat_network.py' -v
"""

import subprocess
import unittest

from compat_support import ( DockerTestCase, BASH_IPROUTE_IMAGE,
                             BASH_MINIMAL_IMAGE, BUSYBOX_IMAGE,
                             containerExists, pingOnce, pingRttMs )
from mininet.link import Intf, TCIntf, TCLink
from mininet.log import setLogLevel
from mininet.net import Containernet
from mininet.netexec import NamespaceRunner

ROUTER_SYSCTLS = { 'net.ipv4.ip_forward': '1' }


class NetworkTestCase( DockerTestCase ):
    """Creates a controller-less Containernet per test and stops it."""

    needsRoot = True
    intfClass = Intf

    def setUp( self ):
        """Create an empty network."""
        setLogLevel( 'warning' )
        self.net = Containernet( controller=None, intf=self.intfClass )

    def tearDown( self ):
        """Stop the network, then remove anything left behind."""
        try:
            self.net.stop()
        finally:
            super().tearDown()

    def addHost( self, name, image, **params ):
        """Add a Docker host, replacing any stale container of that name."""
        return self.net.addDocker( name, dimage=image, rm=True, **params )


class testMixedImages( NetworkTestCase ):
    """Hosts from all three image families on direct links."""

    def setUp( self ):
        super().setUp()
        self.alp = self.addHost( 'alp', BUSYBOX_IMAGE )
        self.ubu = self.addHost( 'ubu', BASH_IPROUTE_IMAGE )
        self.deb = self.addHost( 'deb', BASH_MINIMAL_IMAGE )
        self.net.addLink( self.alp, self.ubu,
                          params1={ 'ip': '10.1.0.1/24' },
                          params2={ 'ip': '10.1.0.2/24' } )
        self.net.addLink( self.alp, self.deb,
                          params1={ 'ip': '10.2.0.1/24' },
                          params2={ 'ip': '10.2.0.2/24' } )
        self.net.start()

    def testDockerNodesUseNsenter( self ):
        for host in ( self.alp, self.ubu, self.deb ):
            self.assertIsInstance( host.netRunner, NamespaceRunner )
            self.assertTrue( host.netRunner.usable(), host.name )

    def testImageWithoutNetworkingToolsIsConfigured( self ):
        self.assertEqual( self.deb.cmd( 'command -v ip ifconfig tc' ), '' )
        self.assertEqual( self.deb.cmd( 'cat /sys/class/net/deb-eth0/operstate' )
                          .strip(), 'up' )
        self.assertEqual( self.deb.defaultIntf().updateIP(), '10.2.0.2' )
        self.assertTrue( pingOnce( self.alp, '10.2.0.2' ) )

    def testBashImageReachable( self ):
        self.assertTrue( pingOnce( self.alp, '10.1.0.2' ) )
        self.assertTrue( pingOnce( self.ubu, '10.1.0.1' ) )

    def testLinkAddressIsNotOverwritten( self ):
        # The first link's address must survive configHosts()
        for host, ip in ( ( self.alp, '10.1.0.1' ), ( self.ubu, '10.1.0.2' ),
                          ( self.deb, '10.2.0.2' ) ):
            self.assertEqual( host.IP(), ip )
            addresses = host.netCmd( 'ip -4 -o address show dev',
                                     host.defaultIntf() )
            self.assertIn( ip + '/24', addresses )
            self.assertNotIn( '10.0.0.', addresses )

    def testAutomaticAddressWithoutLinkAddress( self ):
        extra = self.addHost( 'ext', BUSYBOX_IMAGE )
        self.net.addLink( self.alp, extra )
        self.net.configHosts()
        self.assertTrue( extra.IP().startswith( '10.0.0.' ) )

    def testNeighbourAndRouteHelpers( self ):
        alpIntf = self.alp.intf( 'alp-eth1' )
        alpIntf.updateMAC()
        self.deb.setARP( '10.2.0.1', alpIntf.MAC() )
        self.assertIn( alpIntf.MAC(), self.deb.netCmd( 'ip neigh show 10.2.0.1' ) )
        self.deb.setHostRoute( '10.1.0.1', 'deb-eth0' )
        self.assertIn( '10.1.0.1 dev deb-eth0',
                       self.deb.netCmd( 'ip route show 10.1.0.1' ) )
        self.deb.setDefaultRoute( 'dev deb-eth0 via 10.2.0.1' )
        self.assertIn( 'default via 10.2.0.1',
                       self.deb.netCmd( 'ip route show default' ) )

    def testInterfaceStateControlsConnectivity( self ):
        intf = self.ubu.defaultIntf()
        intf.set_state( 'down' )
        self.assertEqual( intf.get_state(), 'down' )
        self.assertFalse( pingOnce( self.alp, '10.1.0.2', timeout=1 ) )
        intf.set_state( 'up' )
        self.assertEqual( intf.get_state(), 'up' )
        self.assertTrue( pingOnce( self.alp, '10.1.0.2' ) )

    def testConfigLinkStatus( self ):
        self.net.configLinkStatus( 'alp', 'deb', 'down' )
        self.assertEqual( self.deb.defaultIntf().get_state(), 'down' )
        self.net.configLinkStatus( 'alp', 'deb', 'up' )
        self.assertTrue( pingOnce( self.alp, '10.2.0.2' ) )

    def testStopRemovesContainersAndLinks( self ):
        self.net.stop()
        for name in ( 'mn.alp', 'mn.ubu', 'mn.deb' ):
            self.assertFalse( containerExists( name ) )
        hostLinks = subprocess.run( [ 'ip', '-o', 'link', 'show' ],
                                    capture_output=True, text=True ).stdout
        for prefix in ( 'alp-eth', 'ubu-eth', 'deb-eth' ):
            self.assertNotIn( prefix, hostLinks )
        self.net = Containernet( controller=None )   # tearDown stops this


class testTrafficShaping( NetworkTestCase ):
    """tc shaping through the API the GUI uses (intf.config)."""

    intfClass = TCIntf

    def setUp( self ):
        super().setUp()
        self.a = self.addHost( 'ta', BUSYBOX_IMAGE )
        self.b = self.addHost( 'tb', BASH_MINIMAL_IMAGE )   # no tc inside
        self.link = self.net.addLink( self.a, self.b, cls=TCLink,
                                      params1={ 'ip': '10.3.0.1/24' },
                                      params2={ 'ip': '10.3.0.2/24' } )
        self.net.start()

    def shape( self, **params ):
        """Apply the same shaping to both ends, like set_link_tc()."""
        self.link.intf1.config( **params )
        self.link.intf2.config( **params )

    def testDelayAppliesAndResets( self ):
        baseline = pingRttMs( self.a, '10.3.0.2' )
        self.assertIsNotNone( baseline )
        self.shape( delay='50ms' )
        shaped = pingRttMs( self.a, '10.3.0.2' )
        self.assertGreaterEqual( shaped, 95 )
        self.shape()   # no parameters -> default qdisc again
        self.assertLess( pingRttMs( self.a, '10.3.0.2' ), 40 )
        self.assertNotIn( 'netem', self.b.netCmd( 'tc qdisc show dev tb-eth0' ) )

    def testLossCanBeRemoved( self ):
        self.shape( loss=100 )
        self.assertFalse( pingOnce( self.a, '10.3.0.2', timeout=1 ) )
        self.shape( loss=0 )
        self.assertTrue( pingOnce( self.a, '10.3.0.2' ) )

    def testStringParametersFromJson( self ):
        self.shape( delay='20ms', loss='0%', bw='10' )
        qdisc = self.a.netCmd( 'tc qdisc show dev ta-eth0' )
        self.assertIn( 'htb', qdisc )
        self.assertIn( 'delay 20', qdisc )

    def testReshapingKeepsInterfaceDown( self ):
        self.link.intf2.set_state( 'down' )
        self.link.intf2.config( delay='10ms' )
        self.assertEqual( self.link.intf2.get_state(), 'down' )


class testRoutedRedundancy( NetworkTestCase ):
    """d1 - er1 =(primary, backup)= wan - er2 - d2 with static routes.

       Mirrors main_simple_redundancy.py using Ubuntu hosts (as the
       project does), BusyBox routers, the project's ifconfig override and
       TCIntf as the network-wide interface class."""

    intfClass = TCIntf

    @staticmethod
    def ipLinkConf( intf, *args ):
        """The ifconfig replacement used by NetworkDegradationSimulation."""
        return intf.cmd( 'ip', 'link', 'set', 'dev', intf.name, *args )

    def link( self, nodeA, nodeB, ipA, ipB ):
        """Add a link the way network_builder/topology.py does."""
        return self.net.addLink(
            nodeA, nodeB,
            params1={ 'ip': ipA, 'ifconfig': self.ipLinkConf },
            params2={ 'ip': ipB, 'ifconfig': self.ipLinkConf } )

    def setUp( self ):
        super().setUp()
        d1 = self.d1 = self.addHost( 'd1', BASH_IPROUTE_IMAGE )
        d2 = self.d2 = self.addHost( 'd2', BASH_IPROUTE_IMAGE )
        wan = self.wan = self.addHost( 'wan', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        er1 = self.er1 = self.addHost( 'er1', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        er2 = self.er2 = self.addHost( 'er2', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        self.link( d1, er1, '192.168.1.2/24', '192.168.1.1/24' )
        self.link( d2, er2, '192.168.2.2/24', '192.168.2.1/24' )
        self.primary = self.link( er1, wan, '10.0.1.2/30', '10.0.1.1/30' )
        self.link( er1, wan, '20.0.1.2/30', '20.0.1.1/30' )
        self.link( er2, wan, '10.0.2.2/30', '10.0.2.1/30' )
        self.net.start()

        route = 'ip route replace'
        er1.netCmd( route, '192.168.2.0/24 via 10.0.1.1 metric 1' )
        er1.netCmd( 'ip route add 192.168.2.0/24 via 20.0.1.1 metric 2' )
        er2.netCmd( route, '192.168.1.0/24 via 10.0.2.1' )
        wan.netCmd( route, '192.168.1.0/24 via 10.0.1.2 metric 1' )
        wan.netCmd( 'ip route add 192.168.1.0/24 via 20.0.1.2 metric 2' )
        wan.netCmd( route, '192.168.2.0/24 via 10.0.2.2' )
        d1.setDefaultRoute( 'via 192.168.1.1' )
        d2.setDefaultRoute( 'via 192.168.2.1' )

    def egress( self, node, ip ):
        """Return the interface the kernel would use to reach ip."""
        return node.netCmd( 'ip route get', ip ).split( ' dev ' )[ 1 ].split()[ 0 ]

    def testPrivilegedRouters( self ):
        info = self.er1.dcli.inspect_container( self.er1.did )
        self.assertTrue( info[ 'HostConfig' ][ 'Privileged' ] )
        self.assertEqual( self.er1.cmd( 'cat /proc/sys/net/ipv4/ip_forward' )
                          .strip(), '1' )

    def testEndToEnd( self ):
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )
        self.assertEqual( self.egress( self.er1, '192.168.2.2' ), 'er1-eth1' )

    def testFailoverToBackupLink( self ):
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )
        self.primary.intf1.set_state( 'down' )
        self.primary.intf2.set_state( 'down' )
        self.assertEqual( self.egress( self.er1, '192.168.2.2' ), 'er1-eth2' )
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )

    def testDegradedPrimaryAddsLatency( self ):
        baseline = pingRttMs( self.d1, '192.168.2.2' )
        self.primary.intf1.config( delay='40ms' )
        degraded = pingRttMs( self.d1, '192.168.2.2' )
        self.primary.intf1.config()
        restored = pingRttMs( self.d1, '192.168.2.2' )
        self.assertGreaterEqual( degraded, 38 )
        self.assertLess( restored, baseline + 20 )

    def testInterfaceGraphState( self ):
        # topology.py reads the state of every interface after creation
        for host in ( self.d1, self.er1, self.wan ):
            for intf in host.intfList():
                self.assertEqual( intf.get_state(), 'up' )


if __name__ == '__main__':
    unittest.main()
