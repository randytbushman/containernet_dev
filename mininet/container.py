"""
container.py: helpers that make Containernet work with arbitrary images.

ImageReference: parses Docker image references ([registry[:port]/]repo
    [:tag][@digest]) correctly, including registries with ports.
ShellProfile: describes how to start the interactive control shell that
    Containernet drives inside a container. Profiles are registered in
    priority order and detected per container, so images that ship only a
    POSIX sh (Alpine/BusyBox, Debian dash) work as well as bash images.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional


# Appended to a backgrounded command: prints ^A<pid>\n of the new job
PID_MARKER_CMD = ' printf "\\001%d\\012" $! '

# Shells that an image's CMD commonly names just to provide a prompt.
# Containernet already runs its own shell, so these are never re-launched.
INTERACTIVE_SHELLS = frozenset( [ 'bash', '/bin/bash', '/usr/bin/bash',
                                  'sh', '/bin/sh', '/usr/bin/sh',
                                  'ash', '/bin/ash' ] )


@dataclass( frozen=True )
class ImageReference( object ):
    """A parsed Docker image reference."""

    repository: str
    tag: Optional[ str ] = None
    digest: Optional[ str ] = None

    @classmethod
    def parse( cls, reference: str ) -> ImageReference:
        """Parse an image reference string.
           A ':' only denotes a tag if it follows the last '/', so
           'localhost:5000/img' is a repository without a tag.
           reference: e.g. 'ubuntu:trusty', 'reg:5000/team/img@sha256:..'
           returns: ImageReference"""
        name, _, digest = reference.partition( '@' )
        lastSlash = name.rfind( '/' )
        lastColon = name.rfind( ':' )
        if lastColon > lastSlash:
            return cls( name[ :lastColon ], name[ lastColon + 1: ] or None,
                        digest or None )
        return cls( name, None, digest or None )

    @property
    def pullTag( self ) -> str:
        """Tag or digest to request from the registry ('latest' if unset).
           Never empty: an empty tag makes the daemon pull *every* tag."""
        return self.digest or self.tag or 'latest'

    def __str__( self ) -> str:
        """Canonical reference string, defaulting the tag to 'latest'."""
        if self.digest:
            return '%s@%s' % ( self.repository, self.digest )
        return '%s:%s' % ( self.repository, self.tag or 'latest' )


class ShellProfile( ABC ):
    """How to start a sentinel-prompt control shell inside a container.

       Subclasses are registered with ShellProfile.register() in priority
       order; detect() picks the first one available in a container."""

    #: Shell executable name inside the container
    executable: ClassVar[ str ] = ''
    _registry: ClassVar[ dict[ str, ShellProfile ] ] = {}

    @abstractmethod
    def argv( self, label: str ) -> list[ str ]:
        """Return the argv that starts an interactive shell reading stdin.
           label: marker placed in the process' argv (e.g. 'mininet:h1') so
                  stale shells can be found and killed by 'mn -c'"""

    def environment( self, prompt: str ) -> dict[ str, str ]:
        """Environment variables passed to the shell via 'docker exec -e'.
           prompt: sentinel prompt string Mininet waits for"""
        return { 'PS1': prompt }

    def backgroundCommand( self, command: str ) -> str:
        """Return shell text that runs a command ending in '&' and prints
           ^A<pid>\\n, the marker Node.monitor() uses to set lastPid.
           command: command line ending in '&'"""
        return command + PID_MARKER_CMD

    def isAvailableIn( self, container: str ) -> bool:
        """Return whether this shell can run inside a container.
           container: container name or id"""
        try:
            probe = subprocess.run(
                [ 'docker', 'exec', container, self.executable, '-c', 'true' ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=30 )
        except ( OSError, subprocess.TimeoutExpired ):
            return False
        return probe.returncode == 0

    @classmethod
    def register( cls, profile: ShellProfile ) -> None:
        """Register a profile; earlier registrations have priority.
           profile: ShellProfile instance"""
        cls._registry[ profile.executable ] = profile

    @classmethod
    def forName( cls, name: str ) -> ShellProfile:
        """Return the profile registered for a shell name.
           name: shell executable name, e.g. 'bash' or 'sh'"""
        try:
            return cls._registry[ name ]
        except KeyError:
            raise ValueError( 'Unknown shell %r; registered shells: %s'
                              % ( name, ', '.join( cls._registry ) ) )

    @classmethod
    def detect( cls, container: str,
                preferred: Optional[ str ] = None ) -> ShellProfile:
        """Pick the shell to use for a container.
           container: container name or id
           preferred: force a registered shell instead of probing
           returns: ShellProfile
           raises: RuntimeError if the image provides no supported shell"""
        if preferred:
            return cls.forName( preferred )
        for profile in cls._registry.values():
            if profile.isAvailableIn( container ):
                return profile
        raise RuntimeError(
            'Container %s provides none of the supported shells (%s). '
            'Containernet needs a shell inside the image to run commands.'
            % ( container, ', '.join( cls._registry ) ) )


class BashShellProfile( ShellProfile ):
    """GNU bash. Line editing is disabled so readline never emits
       terminal control sequences (e.g. bracketed paste) into output."""

    executable = 'bash'

    def argv( self, label: str ) -> list[ str ]:
        """See ShellProfile.argv."""
        return [ 'bash', '--norc', '--noediting', '-is', label ]


class PosixShellProfile( ShellProfile ):
    """Any POSIX sh (dash, BusyBox ash, ...)."""

    def backgroundCommand( self, command: str ) -> str:
        """Start the job from a subshell that exits immediately.
           BusyBox ash re-prints its prompt (our sentinel) when a job ends
           while it waits for input, which would cut the output of the
           next command short. Detached jobs never notify the control
           shell; the trade-off is that %job references and 'wait' do not
           apply to them (use the PID from lastPid instead).
           command: command line ending in '&'"""
        return '( %s )' % ( command + PID_MARKER_CMD )

    executable = 'sh'

    def argv( self, label: str ) -> list[ str ]:
        """See ShellProfile.argv."""
        return [ 'sh', '-is', label ]

    def environment( self, prompt: str ) -> dict[ str, str ]:
        """See ShellProfile.environment; also stops $ENV start-up files."""
        env = super().environment( prompt )
        env[ 'ENV' ] = ''
        return env


ShellProfile.register( BashShellProfile() )
ShellProfile.register( PosixShellProfile() )
