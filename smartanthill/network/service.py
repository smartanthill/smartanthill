# Copyright (C) Ivan Kravets <me@ikravets.com>
# See LICENSE for details.

# pylint: disable=W0613

from binascii import hexlify

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, maybeDeferred, returnValue
from twisted.internet.serialport import SerialPort

import smartanthill.network.protocol as p
from smartanthill.exception import NetworkRouterConnectFailure
from smartanthill.service import SAMultiService
from smartanthill.util import get_service_named


class ControlService(SAMultiService):

    def __init__(self, name):
        SAMultiService.__init__(self, name)
        self._protocol = p.ControlProtocolWrapping(
            self.climessage_protocallback)
        self._litemq = None

    def startService(self):
        self._litemq = get_service_named("litemq")
        self._protocol.makeConnection(self)
        self._litemq.consume("network", "control.in", "transport->control",
                             self.inmessage_mqcallback)
        self._litemq.consume("network", "control.out", "client->control",
                             self.outmessage_mqcallback)
        SAMultiService.startService(self)

    def stopService(self):
        SAMultiService.stopService(self)
        self._litemq.unconsume("network", "control.in")
        self._litemq.unconsume("network", "control.out")

    def write(self, message):
        self._litemq.produce("network", "control->transport", message,
                             dict(binary=True))

    def inmessage_mqcallback(self, message, properties):
        self.log.debug("Received incoming raw message %s" % hexlify(message))
        self._protocol.dataReceived(message)

    def outmessage_mqcallback(self, message, properties):
        self.log.debug("Received outgoing %s and properties=%s" %
                       (message, properties))
        self._protocol.send_message(message)

    def climessage_protocallback(self, message):
        self.log.debug("Received incoming client %s" % message)
        self._litemq.produce("network", "control->client", message)


class TransportService(SAMultiService):

    def __init__(self, name):
        SAMultiService.__init__(self, name)
        self._protocol = p.TransportProtocolWrapping(
            self.rawmessage_protocallback)
        self._litemq = None

    def startService(self):
        self._litemq = get_service_named("litemq")
        self._protocol.makeConnection(self)
        self._litemq.consume("network", "transport.in", "routing->transport",
                             self.insegment_mqcallback)
        self._litemq.consume("network", "transport.out", "control->transport",
                             self.outmessage_mqcallback, ack=True)
        SAMultiService.startService(self)

    def stopService(self):
        SAMultiService.stopService(self)
        self._litemq.unconsume("network", "transport.in")
        self._litemq.unconsume("network", "transport.out")

    def rawmessage_protocallback(self, message):
        self.log.debug("Received incoming raw message %s" % hexlify(message))
        self._litemq.produce("network", "transport->control", message,
                             dict(binary=True))

    def write(self, segment):
        self._litemq.produce("network", "transport->routing", segment,
                             dict(binary=True))

    def insegment_mqcallback(self, message, properties):
        self.log.debug("Received incoming segment %s" % hexlify(message))
        self._protocol.dataReceived(message)

    @inlineCallbacks
    def outmessage_mqcallback(self, message, properties):
        self.log.debug("Received outgoing message %s" % hexlify(message))
        ctrlmsg = p.ControlProtocol.rawmessage_to_message(message)

        def _on_err(failure):
            self._litemq.produce("network", "transport->err", ctrlmsg)
            failure.raiseException()

        d = maybeDeferred(self._protocol.send_message, message)
        d.addErrback(_on_err)
        result = yield d
        if result and ctrlmsg.ack:
            self._litemq.produce("network", "transport->ack", ctrlmsg)
        returnValue(result)


class RouterService(SAMultiService):

    RECONNECT_DELAY = 1  # in seconds

    def __init__(self, name, options):
        SAMultiService.__init__(self, name, options)
        self._protocol = p.RoutingProtocolWrapping(self.inpacket_protocallback)
        self._litemq = None
        self._reconnect_nums = 0

    def startService(self):
        connection = self.options['connection']
        try:
            if connection.get_type() == "serial":
                _kwargs = connection.params
                # rename port's argument
                _kwargs['deviceNameOrPortNumber'] = _kwargs['port']
                del _kwargs['port']
                _kwargs['protocol'] = self._protocol
                _kwargs['reactor'] = reactor

                SerialPort(**_kwargs)
        except:
            self.log.error(NetworkRouterConnectFailure(self.options))
            self._reconnect_nums += 1
            reactor.callLater(self._reconnect_nums * self.RECONNECT_DELAY,
                              self.startService)
            return

        self._litemq = get_service_named("litemq")
        self._litemq.consume("network", "routing.out", "transport->routing",
                             self.outsegment_mqcallback)
        SAMultiService.startService(self)

    def stopService(self):
        SAMultiService.stopService(self)
        if self._litemq:
            self._litemq.unconsume("network", "routing.out")

    def inpacket_protocallback(self, packet):
        self.log.debug("Received incoming packet %s" % hexlify(packet))
        self._litemq.produce("network", "routing->transport",
                             p.RoutingProtocol.packet_to_segment(packet),
                             dict(binary=True))

    def outsegment_mqcallback(self, message, properties):
        # check destination ID  @TODO
        if ord(message[2]) not in self.options['deviceids']:
            return False
        self.log.debug("Received outgoing segment %s" % hexlify(message))
        self._protocol.send_segment(message)


class NetworkService(SAMultiService):

    def __init__(self, name, options):
        SAMultiService.__init__(self, name, options)
        self._litemq = None

    def startService(self):
        self._litemq = get_service_named("litemq")
        self._litemq.declare_exchange("network")

        ControlService("network.control").setServiceParent(self)
        TransportService("network.transport").setServiceParent(self)

        devices = get_service_named("device").get_devices()
        for devid, devobj in devices.iteritems():
            if not devobj.options.get("router", False):
                continue

            _options = {"connection": devobj.connection, "deviceids": [devid]}
            _options['deviceids'] += [d.id_ for d in devobj.get_nodes()]

            RouterService("network.router.%d" % devid,
                          _options).setServiceParent(self)

        SAMultiService.startService(self)

    def stopService(self):
        SAMultiService.stopService(self)
        self._litemq.undeclare_exchange("network")


def makeService(name, options):
    return NetworkService(name, options)
