# -*- coding: utf-8 -*-
"""Kontalk XMPP router component."""
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

from copy import deepcopy

from twisted.words.protocols.jabber.component import XMPPComponentServerFactory
from twisted.words.protocols.jabber import jid
from twisted.words.xish import domish

from wokkel import component
from wokkel.xmppim import Presence, UnavailablePresence

from kontalk.xmppserver import log


class Router(component.Router):
    """Kontalk router."""

    def __init__(self):
        component.Router.__init__(self)

    def addRoute(self, destination, xs):
        """
        Add a new route.

        The passed XML Stream C{xs} will have an observer for all stanzas
        added to route its outgoing traffic. In turn, traffic for
        C{destination} will be passed to this stream.

        @param destination: Destination of the route to be added as a host name
                            or C{None} for the default route.
        @type destination: C{str} or C{NoneType}.
        @param xs: XML Stream to register the route for.
        @type xs: L{EventDispatcher<utility.EventDispatcher>}.
        """

        stanza = Presence()
        stanza['from'] = destination

        log.debug("adversiting component %s" % (destination, ))
        self.broadcast(stanza)

        # advertise this component about the others
        for host in self.routes.iterkeys():
            stanza = Presence()
            stanza['from'] = host
            xs.send(stanza)

        # add route and observers
        self.routes[destination] = xs
        xs.addObserver('/bind', self.onBind, xs = xs)
        xs.addObserver('/unbind', self.onUnbind, xs = xs)
        xs.addObserver('/*', self.route, xs = xs)

    def removeRoute(self, destination, xs):
        # remove route immediately
        # we assume component is disconnecting so we don't remove observers
        component.Router.removeRoute(self, destination, xs)

        stanza = UnavailablePresence()
        stanza['from'] = destination
        log.debug("unadvertising component %s" % (stanza['from'],))
        self.broadcast(stanza)

    def route(self, stanza, xs):
        """
        Route a stanza.

        @param stanza: The stanza to be routed.
        @type stanza: L{domish.Element}.
        """

        # reset namespace
        stanza.defaultUri = stanza.uri = component.NS_COMPONENT_ACCEPT

        if not stanza.hasAttribute('to'):
            log.debug("broadcasting stanza %s" % (stanza.toXml()))
            self.broadcast(stanza)
        else:
            log.debug("routing stanza %s" % (stanza.toXml()))
            try:
                destination = jid.JID(stanza['to'])

                if destination.host in self.routes:
                    self.routes[destination.host].send(stanza)
                else:
                    self.routes[None].send(stanza)

            except KeyError:
                log.debug("unroutable stanza, bouncing back to component")
                error = domish.Element((None, 'error'))
                error['type'] = 'unroutable'
                error['to'] = stanza['from']
                error.addChild(stanza)
                xs.send(error)

    def broadcast(self, stanza, same=False):
        """Broadcast a stanza to every component."""
        stanza = deepcopy(stanza)
        jid_from = jid.JID(stanza['from'])
        for host, xs in self.routes.iteritems():
            # do not send to the original sender
            if host != jid_from.host or same:
                log.debug("sending to %s" % (host, ))
                stanza['to'] = host
                xs.send(stanza)

    def onBind(self, stanza, xs):
        log.debug("binding component %s" % (stanza.toXml(), ))
        # TODO

    def onUnbind(self, stanza, xs):
        log.debug("unbinding component %s" % (stanza.toXml(), ))
        # TODO


class XMPPRouterFactory(XMPPComponentServerFactory):
    """
    XMPP Component Server factory implementing a routing protocol.
    """

    def __init__(self, router, secret='secret'):
        XMPPComponentServerFactory.__init__(self, router, secret)
