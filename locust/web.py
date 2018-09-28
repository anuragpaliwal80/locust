# -*- coding: utf-8 -*-

import csv
import json
import logging
import os.path
from collections import defaultdict
from itertools import chain
from time import time

import six
from flask import Flask, make_response, jsonify, render_template, request, send_file
from gevent import pywsgi

from locust import __version__ as version
from six.moves import StringIO, xrange

from . import runners
from .runners import MasterLocustRunner
from .stats import distribution_csv, median_from_dict, requests_csv, sort_stats
from .util.cache import memoize

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TIME = 2.0

app = Flask(__name__)
app.debug = True
app.root_path = os.path.dirname(os.path.abspath(__file__))


@app.route('/')
def index():
    is_distributed = isinstance(runners.locust_runner, MasterLocustRunner)
    if is_distributed:
        slave_count = runners.locust_runner.slave_count
    else:
        slave_count = 0

    if runners.locust_runner.host:
        host = runners.locust_runner.host
    elif len(runners.locust_runner.locust_classes) > 0:
        host = runners.locust_runner.locust_classes[0].host
    else:
        host = None

    return render_template("index.html",
        state=runners.locust_runner.state,
        is_distributed=is_distributed,
        user_count=runners.locust_runner.user_count,
        version=version,
        host=host
    )

@app.route('/swarm', methods=["POST"])
def swarm():
    assert request.method == "POST"

    locust_count = int(request.form["locust_count"])
    hatch_rate = float(request.form["hatch_rate"])
    runners.locust_runner.start_hatching(locust_count, hatch_rate)
    return jsonify({'success': True, 'message': 'Swarming started'})

@app.route('/stop')
def stop():
    runners.locust_runner.stop()
    return jsonify({'success':True, 'message': 'Test stopped'})

@app.route("/stats/reset")
def reset_stats():
    runners.locust_runner.stats.reset_all()
    return "ok"

@app.route("/stats/requests/csv")
def request_stats_csv():
    return send_file('/_requests.csv', attachment_filename='requests.csv')

@app.route("/stats/distribution/csv")
def distribution_stats_csv():
    return send_file('/_distribution.csv', attachment_filename='distributions.csv')

@app.route('/stats/requests')
@memoize(timeout=DEFAULT_CACHE_TIME, dynamic_timeout=True)
def request_stats():
    stats = []

    for s in chain(sort_stats(runners.locust_runner.request_stats), [runners.locust_runner.stats.total]):
        stats.append({
            "method": s.method,
            "name": s.name,
            "num_requests": s.num_requests,
            "num_failures": s.num_failures,
            "avg_response_time": s.avg_response_time,
            "min_response_time": s.min_response_time or 0,
            "max_response_time": s.max_response_time,
            "current_rps": s.current_rps,
            "median_response_time": s.median_response_time,
            "avg_content_length": s.avg_content_length,
            "min_server_processing" : s.http_stats.server_processing_time.min_time,
            "max_server_processing" : s.http_stats.server_processing_time.max_time,
            "min_dns_lookup" : s.http_stats.dns_lookup_time.min_time,
            "max_dns_lookup" : s.http_stats.dns_lookup_time.max_time,
            "min_tcp_connection" : s.http_stats.tcp_connection_time.min_time,
            "max_tcp_connection" : s.http_stats.tcp_connection_time.max_time,
            "min_pre_transfer" : s.http_stats.pre_transfer_time.min_time,
            "max_pre_transfer" : s.http_stats.pre_transfer_time.max_time,
            "min_connect" : s.http_stats.connect_time.min_time,
            "max_connect" : s.http_stats.connect_time.max_time,
            "min_start_transfer" : s.http_stats.start_transfer_time.min_time,
            "max_start_transfer" : s.http_stats.start_transfer_time.max_time,
            "min_tls_handshake" : s.http_stats.tls_handshake_time.min_time,
            "max_tls_handshake" : s.http_stats.tls_handshake_time.max_time,
            "min_name_lookup" : s.http_stats.name_lookup_time.min_time,
            "max_name_lookup" : s.http_stats.name_lookup_time.max_time,
        })

    errors = [e.to_dict() for e in six.itervalues(runners.locust_runner.errors)]

    # Truncate the total number of stats and errors displayed since a large number of rows will cause the app
    # to render extremely slowly. Aggregate stats should be preserved.
    report = {"stats": stats[:500], "errors": errors[:500]}

    if stats:
        report["total_rps"] = stats[len(stats)-1]["current_rps"]
        report["fail_ratio"] = runners.locust_runner.stats.total.fail_ratio
        report["current_response_time_percentile_95"] = runners.locust_runner.stats.total.get_current_response_time_percentile(0.95)
        report["current_response_time_percentile_50"] = runners.locust_runner.stats.total.get_current_response_time_percentile(0.5)
        report["current_response_time_percentile_99"] = runners.locust_runner.stats.total.get_current_response_time_percentile(0.99)

    is_distributed = isinstance(runners.locust_runner, MasterLocustRunner)
    if is_distributed:
        slaves = []
        for slave in runners.locust_runner.clients.values():
            slaves.append({"id":slave.id, "state":slave.state, "user_count": slave.user_count})

        report["slaves"] = slaves

    report["state"] = runners.locust_runner.state
    report["user_count"] = runners.locust_runner.user_count

    return jsonify(report)

@app.route("/exceptions")
def exceptions():
    return jsonify({
        'exceptions': [
            {
                "count": row["count"],
                "msg": row["msg"],
                "traceback": row["traceback"],
                "nodes" : ", ".join(row["nodes"])
            } for row in six.itervalues(runners.locust_runner.exceptions)
        ]
    })

@app.route("/exceptions/csv")
def exceptions_csv():
    data = StringIO()
    writer = csv.writer(data)
    writer.writerow(["Count", "Message", "Traceback", "Nodes"])
    for exc in six.itervalues(runners.locust_runner.exceptions):
        nodes = ", ".join(exc["nodes"])
        writer.writerow([exc["count"], exc["msg"], exc["traceback"], nodes])

    data.seek(0)
    response = make_response(data.read())
    file_name = "exceptions_{0}.csv".format(time())
    disposition = "attachment;filename={0}".format(file_name)
    response.headers["Content-type"] = "text/csv"
    response.headers["Content-disposition"] = disposition
    return response

def start(locust, options):
    pywsgi.WSGIServer((options.web_host, options.port),
                      app, log=None).serve_forever()
