# -*- coding: utf-8 -*-
"""TLS powered by GnuTLS."""
"""
  Kontalk XMPP server
  Copyright (C) 2011 Kontalk Devteam <devteam@kontalk.org>

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


from twisted.protocols.policies import WrappingFactory, ProtocolWrapper
from twisted.internet.interfaces import ILoggingContext, ISystemHandle, ISSLTransport
from twisted.internet.protocol import Protocol
from twisted.python.failure import Failure
from twisted.internet.main import CONNECTION_LOST

# WARNING internal import
from twisted.internet._newtls import _BypassTLS

from zope.interface import implements, providedBy, directlyProvides

from gnutls.connection import *
from gnutls.errors import *


def startTLS(transport, credentials, normal, bypass):
    """
    Add a layer of SSL to a transport.

    @param transport: The transport which will be modified.  This can either by
        a L{FileDescriptor<twisted.internet.abstract.FileDescriptor>} or a
        L{FileHandle<twisted.internet.iocpreactor.abstract.FileHandle>}.  The
        actual requirements of this instance are that it have:

          - a C{_tlsClientDefault} attribute indicating whether the transport is
            a client (C{True}) or a server (C{False})
          - a settable C{TLS} attribute which can be used to mark the fact
            that SSL has been started
          - settable C{getHandle} and C{getPeerCertificate} attributes so
            these L{ISSLTransport} methods can be added to it
          - a C{protocol} attribute referring to the L{IProtocol} currently
            connected to the transport, which can also be set to a new
            L{IProtocol} for the transport to deliver data to

    @param credentials: A TLSCredentials object.
    @type contextFactory: L{gnutls.connection.TLSCredentials}

    @param normal: A flag indicating whether SSL will go in the same direction
        as the underlying transport goes.  That is, if the SSL client will be
        the underlying client and the SSL server will be the underlying server.
        C{True} means it is the same, C{False} means they are switched.
    @type param: L{bool}

    @param bypass: A transport base class to call methods on to bypass the new
        SSL layer (so that the SSL layer itself can send its bytes).
    @type bypass: L{type}
    """
    # Figure out which direction the SSL goes in.  If normal is True,
    # we'll go in the direction indicated by the subclass.  Otherwise,
    # we'll go the other way (client = not normal ^ _tlsClientDefault,
    # in other words).
    if normal:
        client = transport._tlsClientDefault
    else:
        client = not transport._tlsClientDefault

    # If we have a producer, unregister it, and then re-register it below once
    # we've switched to TLS mode, so it gets hooked up correctly:
    producer, streaming = None, None
    if transport.producer is not None:
        producer, streaming = transport.producer, transport.streamingProducer
        transport.unregisterProducer()

    tlsFactory = TLSSessionFactory(credentials, client, None)
    tlsProtocol = TLSSessionProtocol(tlsFactory, transport.protocol, False)
    transport.protocol = tlsProtocol

    transport.getHandle = tlsProtocol.getHandle
    transport.getPeerCertificate = tlsProtocol.getPeerCertificate

    # Mark the transport as secure.
    directlyProvides(transport, ISSLTransport)

    # Remember we did this so that write and writeSequence can send the
    # data to the right place.
    transport.TLS = True

    # Hook it up
    transport.protocol.makeConnection(_BypassTLS(bypass, transport))

    # Restore producer if necessary:
    if producer:
        transport.registerProducer(producer, streaming)


class TLSSessionProtocol(ProtocolWrapper):
    """
    @ivar _tlsConnection: The L{OpenSSL.SSL.Connection} instance which is
        encrypted and decrypting this connection.

    @ivar _lostTLSConnection: A flag indicating whether connection loss has
        already been dealt with (C{True}) or not (C{False}). TLS disconnection
        is distinct from the underlying connection being lost.

    @ivar _writeBlockedOnRead: A flag indicating whether further writing must
        wait for data to be received (C{True}) or not (C{False}).

    @ivar _appSendBuffer: A C{list} of C{str} of application-level (cleartext)
        data which is waiting for C{_writeBlockedOnRead} to be reset to
        C{False} so it can be passed to and perhaps accepted by
        C{_tlsConnection.send}.

    @ivar _connectWrapped: A flag indicating whether or not to call
        C{makeConnection} on the wrapped protocol.  This is for the reactor's
        L{twisted.internet.interfaces.ITLSTransport.startTLS} implementation,
        since it has a protocol which it has already called C{makeConnection}
        on, and which has no interest in a new transport.  See #3821.

    @ivar _handshakeDone: A flag indicating whether or not the handshake is
        known to have completed successfully (C{True}) or not (C{False}).  This
        is used to control error reporting behavior.  If the handshake has not
        completed, the underlying L{OpenSSL.SSL.Error} will be passed to the
        application's C{connectionLost} method.  If it has completed, any
        unexpected L{OpenSSL.SSL.Error} will be turned into a
        L{ConnectionLost}.  This is weird; however, it is simply an attempt at
        a faithful re-implementation of the behavior provided by
        L{twisted.internet.ssl}.

    @ivar _reason: If an unexpected L{OpenSSL.SSL.Error} occurs which causes
        the connection to be lost, it is saved here.  If appropriate, this may
        be used as the reason passed to the application protocol's
        C{connectionLost} method.

    @ivar _producer: The current producer registered via C{registerProducer},
        or C{None} if no producer has been registered or a previous one was
        unregistered.
    """
    implements(ISystemHandle, ISSLTransport)

    _reason = None
    _handshakeDone = False
    _lostTLSConnection = False
    _writeBlockedOnRead = False
    _producer = None

    def __init__(self, factory, wrappedProtocol, _connectWrapped=True):
        ProtocolWrapper.__init__(self, factory, wrappedProtocol)
        self._connectWrapped = _connectWrapped


    def getHandle(self):
        """
        Return the L{OpenSSL.SSL.Connection} object being used to encrypt and
        decrypt this connection.

        This is done for the benefit of L{twisted.internet.ssl.Certificate}'s
        C{peerFromTransport} and C{hostFromTransport} methods only.  A
        different system handle may be returned by future versions of this
        method.
        """
        return self._tlsConnection


    def makeConnection(self, transport):
        """
        Connect this wrapper to the given transport and initialize the
        necessary L{OpenSSL.SSL.Connection} with a memory BIO.
        """
        tlsContext = self.factory._credentials
        if self.factory._isClient:
            klass = ClientSession
        else:
            klass = ServerSession

        self._tlsConnection = klass(transport.socket, tlsContext)
        self._appSendBuffer = []

        # Add interfaces provided by the transport we are wrapping:
        for interface in providedBy(transport):
            directlyProvides(self, interface)

        # Intentionally skip ProtocolWrapper.makeConnection - it might call
        # wrappedProtocol.makeConnection, which we want to make conditional.
        Protocol.makeConnection(self, transport)
        self.factory.registerProtocol(self)
        if self._connectWrapped:
            # Now that the TLS layer is initialized, notify the application of
            # the connection.
            ProtocolWrapper.makeConnection(self, transport)

        # Now that we ourselves have a transport (initialized by the
        # ProtocolWrapper.makeConnection call above), kick off the TLS
        # handshake.
        try:
            self._tlsConnection.handshake()
        except (OperationWouldBlock, OperationInterrupted):
            self._flushSend()


    def _flushSend(self):
        """
        Read any bytes out of the send BIO and write them to the underlying
        transport.
        """
        try:
            bytes = self._tlsConnection.recv(2 ** 15)
        except (OperationWouldBlock, OperationInterrupted):
            # There may be nothing in the send BIO right now.
            pass
        else:
            self.transport.write(bytes)


    def _flushReceive(self):
        """
        Try to receive any application-level bytes which are now available
        because of a previous write into the receive BIO.  This will take
        care of delivering any application-level bytes which are received to
        the protocol, as well as handling of the various exceptions which
        can come from trying to get such bytes.
        """
        # Keep trying this until an error indicates we should stop or we
        # close the connection.  Looping is necessary to make sure we
        # process all of the data which was put into the receive BIO, as
        # there is no guarantee that a single recv call will do it all.
        while not self._lostTLSConnection:
            try:
                bytes = self._tlsConnection.recv(2 ** 15)
            except (OperationWouldBlock, OperationInterrupted):
                # The newly received bytes might not have been enough to produce
                # any application data.
                break
            # TODO dummy exception
            except ValueError:
                # TLS has shut down and no more TLS data will be received over
                # this connection.
                self._shutdownTLS()
                # Passing in None means the user protocol's connnectionLost
                # will get called with reason from underlying transport:
                self._tlsShutdownFinished(None)
            except GNUTLSError, e:
                # Something went pretty wrong.  For example, this might be a
                # handshake failure (because there were no shared ciphers, because
                # a certificate failed to verify, etc).  TLS can no longer proceed.

                # Squash EOF in violation of protocol into ConnectionLost; we
                # create Failure before calling _flushSendBio so that no new
                # exception will get thrown in the interim.
                if e.args[0] == -1 and e.args[1] == 'Unexpected EOF':
                    failure = Failure(CONNECTION_LOST)
                else:
                    failure = Failure()

                self._flushSend()
                self._tlsShutdownFinished(failure)
            else:
                # If we got application bytes, the handshake must be done by
                # now.  Keep track of this to control error reporting later.
                self._handshakeDone = True
                ProtocolWrapper.dataReceived(self, bytes)

        # The received bytes might have generated a response which needs to be
        # sent now.  For example, the handshake involves several round-trip
        # exchanges without ever producing application-bytes.
        self._flushSend()


    def dataReceived(self, bytes):
        """
        Deliver any received bytes to the receive BIO and then read and deliver
        to the application any application-level data which becomes available
        as a result of this.
        """
        self._tlsConnection.send(bytes)

        if self._writeBlockedOnRead:
            # A read just happened, so we might not be blocked anymore.  Try to
            # flush all the pending application bytes.
            self._writeBlockedOnRead = False
            appSendBuffer = self._appSendBuffer
            self._appSendBuffer = []
            for bytes in appSendBuffer:
                self._write(bytes)
            if (not self._writeBlockedOnRead and self.disconnecting and
                self.producer is None):
                self._shutdownTLS()
            if self._producer is not None:
                self._producer.resumeProducing()

        self._flushReceive()


    def _shutdownTLS(self):
        """
        Initiate, or reply to, the shutdown handshake of the TLS layer.
        """
        shutdownSuccess = self._tlsConnection.bye()
        self._flushSend()
        if shutdownSuccess:
            # Both sides have shutdown, so we can start closing lower-level
            # transport. This will also happen if we haven't started
            # negotiation at all yet, in which case shutdown succeeds
            # immediately.
            self.transport.loseConnection()


    def _tlsShutdownFinished(self, reason):
        """
        Called when TLS connection has gone away; tell underlying transport to
        disconnect.
        """
        self._reason = reason
        self._lostTLSConnection = True
        # Using loseConnection causes the application protocol's
        # connectionLost method to be invoked non-reentrantly, which is always
        # a nice feature. However, for error cases (reason != None) we might
        # want to use abortConnection when it becomes available. The
        # loseConnection call is basically tested by test_handshakeFailure.
        # At least one side will need to do it or the test never finishes.
        self.transport.loseConnection()


    def connectionLost(self, reason):
        """
        Handle the possible repetition of calls to this method (due to either
        the underlying transport going away or due to an error at the TLS
        layer) and make sure the base implementation only gets invoked once.
        """
        if not self._lostTLSConnection:
            # Tell the TLS connection that it's not going to get any more data
            # and give it a chance to finish reading.
            self._tlsConnection.bye()
            self._flushReceive()
            self._lostTLSConnection = True
        reason = self._reason or reason
        self._reason = None
        ProtocolWrapper.connectionLost(self, reason)


    def loseConnection(self):
        """
        Send a TLS close alert and close the underlying connection.
        """
        if self.disconnecting:
            return
        self.disconnecting = True
        if not self._writeBlockedOnRead and self._producer is None:
            self._shutdownTLS()


    def write(self, bytes):
        """
        Process the given application bytes and send any resulting TLS traffic
        which arrives in the send BIO.

        If C{loseConnection} was called, subsequent calls to C{write} will
        drop the bytes on the floor.
        """
        # Writes after loseConnection are not supported, unless a producer has
        # been registered, in which case writes can happen until the producer
        # is unregistered:
        if self.disconnecting and self._producer is None:
            return
        self._write(bytes)


    def _write(self, bytes):
        """
        Process the given application bytes and send any resulting TLS traffic
        which arrives in the send BIO.

        This may be called by C{dataReceived} with bytes that were buffered
        before C{loseConnection} was called, which is why this function
        doesn't check for disconnection but accepts the bytes regardless.
        """
        if self._lostTLSConnection:
            return

        leftToSend = bytes
        while leftToSend:
            try:
                sent = self._tlsConnection.send(leftToSend)
            except WantReadError:
                self._writeBlockedOnRead = True
                self._appSendBuffer.append(leftToSend)
                if self._producer is not None:
                    self._producer.pauseProducing()
                break
            except Error:
                # Pretend TLS connection disconnected, which will trigger
                # disconnect of underlying transport. The error will be passed
                # to the application protocol's connectionLost method.  The
                # other SSL implementation doesn't, but losing helpful
                # debugging information is a bad idea.
                self._tlsShutdownFinished(Failure())
                break
            else:
                # If we sent some bytes, the handshake must be done.  Keep
                # track of this to control error reporting behavior.
                self._handshakeDone = True
                self._flushSend()
                leftToSend = leftToSend[sent:]


    def writeSequence(self, iovec):
        """
        Write a sequence of application bytes by joining them into one string
        and passing them to L{write}.
        """
        self.write("".join(iovec))


    def getPeerCertificate(self):
        return self._tlsConnection.get_peer_certificate()


    def registerProducer(self, producer, streaming):
        # If we've already disconnected, nothing to do here:
        if self._lostTLSConnection:
            producer.stopProducing()
            return

        # If we received a non-streaming producer, wrap it so it becomes a
        # streaming producer:
        if not streaming:
            producer = streamingProducer = _PullToPush(producer, self)
        producer = _ProducerMembrane(producer)
        # This will raise an exception if a producer is already registered:
        self.transport.registerProducer(producer, True)
        self._producer = producer
        # If we received a non-streaming producer, we need to start the
        # streaming wrapper:
        if not streaming:
            streamingProducer.startStreaming()


    def unregisterProducer(self):
        # If we received a non-streaming producer, we need to stop the
        # streaming wrapper:
        if isinstance(self._producer._producer, _PullToPush):
            self._producer._producer.stopStreaming()
        self._producer = None
        self._producerPaused = False
        self.transport.unregisterProducer()
        if self.disconnecting and not self._writeBlockedOnRead:
            self._shutdownTLS()


class TLSSessionFactory(WrappingFactory):
    """
    L{TLSSessionFactory} adds TLS to connections.

    @ivar _credeentials: TLS credentials.

    @ivar _isClient: A flag which is C{True} if this is a client TLS
        connection, C{False} if it is a server TLS connection.
    """
    protocol = TLSSessionProtocol

    noisy = False  # disable unnecessary logging.

    def __init__(self, credentials, isClient, wrappedFactory):
        WrappingFactory.__init__(self, wrappedFactory)
        self._credentials = credentials
        self._isClient = isClient

    def logPrefix(self):
        """
        Annotate the wrapped factory's log prefix with some text indicating TLS
        is in use.

        @rtype: C{str}
        """
        if ILoggingContext.providedBy(self.wrappedFactory):
            logPrefix = self.wrappedFactory.logPrefix()
        else:
            logPrefix = self.wrappedFactory.__class__.__name__
        return "%s (TLS)" % (logPrefix,)