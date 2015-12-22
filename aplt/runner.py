"""Scenario Runner"""
import importlib
import sys
from collections import deque

import treq
from autobahn.twisted.websocket import (
    connectWS,
    WebSocketClientFactory
)
from docopt import docopt
from twisted.internet import reactor, ssl, task
from twisted.python import log
from txstatsd.client import StatsDClientProtocol, TwistedStatsDClient
from txstatsd.metrics.metrics import Metrics

from aplt import __version__
from aplt.client import (
    CommandProcessor,
    WSClientProtocol
)

# Necessary for latest version of txaio
import txaio
txaio.use_twisted()

STATS_PROTOCOL = None


class RunnerHarness(object):
    """Runs multiple instances of a single scenario

    Running an instance of the scenario is triggered with :meth:`run`. It
    will run to completion or possibly forever.

    """
    def __init__(self, websocket_url, statsd_client, scenario, *scenario_args):
        self._factory = WebSocketClientFactory(
            websocket_url,
            headers={"Origin": "localhost:9000"},
            debug=False)
        self._factory.protocol = WSClientProtocol
        self._factory.protocol.harness = self
        if websocket_url.startswith("wss"):
            self._factory_context = ssl.ClientContextFactory()
        else:
            self._factory_context = None

        self._crypto_key = """\
keyid="http://example.org/bob/keys/123;salt="XZwpw6o37R-6qoZjw6KwAw"\
"""

        # Processor and Websocket client vars
        self._scenario = scenario
        self._scenario_args = scenario_args
        self._processors = 0
        self._ws_clients = {}
        self._connect_waiters = deque()
        self._stat_client = statsd_client

    def run(self):
        """Start registered scenario"""
        # Create the processor and start it
        processor = CommandProcessor(self._scenario, self._scenario_args, self)
        processor.run()
        self._processors += 1

    def connect(self, processor):
        """Start a connection for a processor and queue it for when the
        connection is available"""
        connectWS(self._factory, contextFactory=self._factory_context)
        self._connect_waiters.append(processor)

    def send_notification(self, processor, url, data, ttl):
        """Send out a notification to a url for a processor"""
        url = url.encode("utf-8")
        if data:
            d = treq.post(
                url,
                data,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Encoding": "aesgcm-128",
                    "Encryption": self._crypto_key,
                    "Encryption-Key": "Invalid-Key-Used-Here",
                },
                allow_redirects=False)
        else:
            d = treq.post(url, allow_redirects=False)
        d.addCallback(self._sent_notification, processor)
        d.addErrback(self._error_notif, processor)

    def _sent_notification(self, result, processor):
        d = result.content()
        d.addCallback(self._finished_notification, result, processor)
        d.addErrback(self._error_notif, result, processor)

    def _finished_notification(self, result, response, processor):
        # Give the fully read content and response to the processor
        processor._send_command_result((response, result))

    def _error_notif(self, failure, processor):
        # Send the failure back
        processor._send_command_result((None, failure))

    def add_client(self, ws_client):
        """Register a new websocket connection and return a waiting
        processor"""
        try:
            processor = self._connect_waiters.popleft()
        except IndexError:
            log.msg("No waiting processors for new client connection.")
            ws_client.sendClose()
        else:
            self._ws_clients[ws_client] = processor
            return processor

    def remove_client(self, ws_client):
        """Remove a websocket connection from the client registry"""
        processor = self._ws_clients.pop(ws_client, None)
        if not processor:
            # Possible failed connection, if we have waiting processors still
            # then try a new connection
            if len(self._connect_waiters):
                connectWS(self._factory, contextFactory=self._factory_context)
            return

    def remove_processor(self):
        """Remove a completed processor"""
        self._processors -= 1

    def timer(self, name, duration):
        """Record a metric timer if we have a statsd client"""
        self._stat_client.timing(name, duration)

    def counter(self, name, count=1):
        """Record a counter if we have a statsd client"""
        self._stat_client.increment(name, count)


class LoadRunner(object):
        """Runs a bunch of scenarios for a load-test"""
        def __init__(self, scenario_list, statsd_client):
            """Initializes a LoadRunner

            Takes a list of tuples indicating scenario to run, quantity,
            stagger delay, and overall delay.

            Stagger delay is a number indicating how many of the scenario to
            launch per second.

            Overall delay is how many seconds after the start of the load-run
            before the scenario should begin.

            Example::

                lr = LoadRunner([
                    (basic, 1000, 100, 0, *scenario_args),
                ])

            .. note::

                Any leftover quantity not cleanly divided into the stagger
                delay will not be started. The quantity should be cleanly
                divided into stagger delay.

            """
            self._harnesses = []
            self._testplans = scenario_list
            self._started = False
            self._queued_calls = 0
            self._statsd_client = statsd_client

        def start(self, websocket_url):
            """Schedules all the scenarios supplied"""
            for scenario, quantity, stagger, overall_delay in self._testplans:
                harness = RunnerHarness(websocket_url, self._statsd_client,
                                        scenario)
                self._harnesses.append(harness)
                iterations = quantity / stagger
                for delay in range(iterations):
                    def runall():
                        for _ in range(stagger):
                            harness.run()
                        self._queued_calls -= 1
                    self._queued_calls += 1
                    reactor.callLater(overall_delay+delay, runall)
            self._started = True

        @property
        def finished(self):
            """Indicates whether or not the LoadRunner started, has run all the
            calls it queued, and all the processors have finished"""
            return all([
                self._started,
                self._queued_calls == 0,
                all([x._processors == 0 for x in self._harnesses])
            ])


def create_statsd_client(host="localhost", port=8125, namespace="aplt"):
    global STATS_PROTOCOL
    client = TwistedStatsDClient(host, port)
    protocol = StatsDClientProtocol(client)
    STATS_PROTOCOL = reactor.listenUDP(0, protocol)
    return Metrics(connection=client, namespace=namespace)


def check_processors(harness):
    """Task to shut down the reactor if there are no processors running"""
    if harness._processors == 0:
        STATS_PROTOCOL.stopListening()
        reactor.stop()


def check_loadrunner(load_runner):
    """Task to shut down the reactor when the load runner has finished"""
    if load_runner.finished:
        STATS_PROTOCOL.stopListening()
        reactor.stop()


def locate_function(func_name):
    """Locates and loads a function by the string name similar to an entry
    points

    Format of func_name: <package/module>:<function>

    """
    if ":" not in func_name:
        raise Exception("Missing function designation")
    mod, func_name = func_name.split(":")
    module = importlib.import_module(mod)
    scenario = getattr(module, func_name)
    return scenario


def try_int_list_coerce(lst):
    """Attempt to coerce all the elements of a list to ints and return it"""
    new_lst = []
    for p in lst:
        try:
            new_lst.append(int(p))
        except ValueError:
            new_lst.append(p)
    return new_lst


def parse_testplan(testplan):
    """Parse a test plan string into an array of tuples"""
    plans = testplan.split("|")
    result = []
    for plan in plans:
        parts = [x.strip() for x in plan.strip().split(",")]
        func_name = parts.pop(0)
        func = locate_function(func_name)
        args = [func] + try_int_list_coerce(parts)
        result.append(tuple(args))
    return result


def parse_statsd_args(args):
    """Parses statsd args out of a docopt arguments dict and returns a statsd
    client or None"""
    host = args.get("STATSD_HOST") or "localhost"
    port = int(args.get("STATSD_PORT") or 8125)
    namespace = args.get("STATSD_NAMESPACE") or "aplt"
    return create_statsd_client(host, port, namespace)


def run_scenario(args=None, run=True):
    """Run a scenario

    Usage:
        aplt_scenario WEBSOCKET_URL SCENARIO_FUNCTION [SCENARIO_ARGS ...]
                      [--statsd_host STATSD_HOST]
                      [--statsd_port STATSD_PORT]
                      [--statsd_namespace STATSD_NAMESPACE]

    """
    arguments = args or docopt(run_scenario.__doc__, version=__version__)
    arg = arguments["SCENARIO_FUNCTION"]
    scenario = locate_function(arg)
    log.startLogging(sys.stdout)
    statsd_client = parse_statsd_args(arguments)
    scenario_args = try_int_list_coerce(arguments["SCENARIO_ARGS"])
    h = RunnerHarness(arguments["WEBSOCKET_URL"], statsd_client, scenario,
                      *scenario_args)
    h.run()

    if run:
        l = task.LoopingCall(check_processors, h)
        reactor.callLater(1, l.start, 1)
        reactor.run()
    else:
        return h


def run_testplan(args=None, run=True):
    """Run a testplan

    Usage:
        aplt_testplan WEBSOCKET_URL TEST_PLAN
                      [--statsd_host STATSD_HOST]
                      [--statsd_port STATSD_PORT]
                      [--statsd_namespace STATSD_NAMESPACE]

    test_plan should be a string with the following format:
        "<scenario_function>, <quantity>, <stagger>, <delay>, *args | *repeat"

    scenario_function
        String indicating function for the scenario, ex: aplt.scenarios:basic

    quantity
        Integer quantity of instances of the scenario to launch

    stagger
        How many to launch per second up to <quantity> total

    delay
        How long to wait from when the test begins before this portion runs

    *args
        Any optional additional arguments to be supplied to the scenario. The
        argument will be coerced to an integer if possible.

    *repeat
        More tuples of the same format.

    """
    arguments = args or docopt(run_testplan.__doc__, version=__version__)
    testplans = parse_testplan(arguments["TEST_PLAN"])
    statsd_client = parse_statsd_args(arguments)
    lh = LoadRunner(testplans, statsd_client)
    log.startLogging(sys.stdout)
    lh.start(arguments["WEBSOCKET_URL"])

    if run:
        l = task.LoopingCall(check_loadrunner, lh)
        reactor.callLater(1, l.start, 1)
        reactor.run()
    else:
        return lh