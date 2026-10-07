"""
compat_support.py: shared helpers for the container compatibility tests.

The tests are split by the privileges they need:

test_compat_unit.py     pure logic, no Docker, no root
test_compat_docker.py   Docker nodes without networking, Docker access only
test_compat_network.py  full topologies (veth links, nsenter, tc), root

Run everything with
    sudo python3 -m unittest discover -s mininet/test -p 'test_compat_*.py' -v
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from typing import Optional

# Images chosen to cover the three common userland families:
# bash + iproute2, bash without any networking tools, BusyBox sh only.
BASH_IPROUTE_IMAGE = 'ubuntu:trusty'
BASH_MINIMAL_IMAGE = 'debian:bookworm-slim'
BUSYBOX_IMAGE = 'alpine:3.20'
TEST_IMAGES = ( BASH_IPROUTE_IMAGE, BASH_MINIMAL_IMAGE, BUSYBOX_IMAGE )

CONTAINER_LABEL = 'com.containernet'


def dockerAvailable() -> bool:
    """Return whether the Docker daemon is reachable by this user."""
    try:
        return subprocess.run( [ 'docker', 'info' ], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               check=False ).returncode == 0
    except OSError:
        return False


def isRoot() -> bool:
    """Return whether the tests run with root privileges."""
    return os.geteuid() == 0


def ensureImages( images=TEST_IMAGES ) -> None:
    """Pull test images that are not present locally.
       images: image references"""
    for image in images:
        present = subprocess.run( [ 'docker', 'image', 'inspect', image ],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, check=False )
        if present.returncode != 0:
            subprocess.run( [ 'docker', 'pull', '-q', image ], check=True,
                            stdout=subprocess.DEVNULL )


def containerExists( name: str ) -> bool:
    """Return whether a container with exactly this name exists.
       name: container name, e.g. 'mn.h1'"""
    return subprocess.run( [ 'docker', 'inspect', '--type', 'container', name ],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False ).returncode == 0


def removeTestContainers() -> None:
    """Force-remove every Containernet container (label based)."""
    ids = subprocess.run( [ 'docker', 'ps', '-aq', '--filter',
                            'label=%s' % CONTAINER_LABEL ],
                          capture_output=True, text=True,
                          check=False ).stdout.split()
    if ids:
        subprocess.run( [ 'docker', 'rm', '-f' ] + ids,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False )


def pingOnce( node, ip: str, timeout: int = 2 ) -> bool:
    """Send one ICMP echo from a BusyBox/iputils node.
       node: Mininet node with a ping binary
       ip: destination address
       returns: True if a reply arrived"""
    out = node.cmd( 'ping -c 1 -W %d %s >/dev/null 2>&1; echo rc=$?'
                    % ( timeout, ip ) )
    return 'rc=0' in out


def pingRttMs( node, ip: str, count: int = 3 ) -> Optional[ float ]:
    """Return the average ping RTT in milliseconds, or None on failure.
       node: Mininet node with a ping binary
       ip: destination address
       count: number of echo requests"""
    out = node.cmd( 'ping -c %d -W 2 %s' % ( count, ip ) )
    match = re.search( r'= [\d.]+/([\d.]+)/', out )
    return float( match.group( 1 ) ) if match else None


class DockerTestCase( unittest.TestCase ):
    """Base class: skips without Docker and removes left-over containers."""

    needsRoot = False

    @classmethod
    def setUpClass( cls ) -> None:
        """Skip the class if prerequisites are missing; pull images."""
        if not dockerAvailable():
            raise unittest.SkipTest( 'Docker daemon not reachable' )
        if cls.needsRoot and not isRoot():
            raise unittest.SkipTest( 'requires root (run with sudo)' )
        ensureImages()
        removeTestContainers()

    def tearDown( self ) -> None:
        """Never leak containers between tests."""
        removeTestContainers()
