import os
import sys
import time
import logging
import requests
import inspect
import datetime
import traceback
import urllib.parse as urlparse
from collections import OrderedDict

import requests
import numpy as np

from prominterface import PromClient, Context # Use local import for debugging, testing
from supremm.plugin import loadpreprocessors, loadplugins, NodeMetadata
from supremm.summarize import Summarize


VERSION = "2.0.0"
TIMESERIES_VERSION = 4

MAX_CHUNK = 4 #hours

class NodeMeta(NodeMetadata):
    """ container for node metadata """
    def __init__(self, nodename, idx):
        self._nodename = nodename
        self._nodeidx = idx

    nodename = property(lambda self: self._nodename)
    nodeindex = property(lambda self: self._nodeidx)

class PromSummarize(Summarize):
    def __init__(self,  preprocessors, analytics, job, config, mapping, chunk=MAX_CHUNK):
        super(PromSummarize, self).__init__(preprocessors, analytics, job, config)
        self.start = time.time()

        self.chunk_size = chunk

        # Translation PCP -> Prometheus metric names
        self.mapping = mapping
        self.mapping.currentjob = job
        self.nodes_processed = 0

        # TODO use config query to parse yaml scrape interval config
        self.timestep = "30s"

    def get(self):
        """ Return a dict with the summary information """
        output = {}
        timeseries = {}

        je = self.job.get_errors()
        if len(je) > 0:
            self.adderror("job", je)

        if self.job.nodecount > 0:
            for analytic in self.alltimestamps:
                if analytic.status != "uninitialized":
                    if analytic.mode == "all":
                        output[analytic.name] = analytic.results()
                    if analytic.mode == "timeseries":
                        timeseries[analytic.name] = analytic.results()
            for analytic in self.firstlast:
                if analytic.status != "uninitialized":
                    output[analytic.name] = analytic.results()
                    
        output['summarization'] = {
            "version": VERSION,
            "elapsed": time.time() - self.start,
            "created": time.time(),
            "srcdir": self.job.jobdir,
            "complete": self.complete()}

        output['created'] = datetime.datetime.utcnow()

        output['acct'] = self.job.acct
        output['acct']['id'] = self.job.job_id

        #TODO replace job.nodearchives
        if len(timeseries) > 0:
            timeseries['hosts'] = dict((str(idx), name) for idx, name in enumerate(self.job.nodenames()))
            timeseries['version'] = TIMESERIES_VERSION
            output['timeseries'] = timeseries

        for preproc in self.preprocs:
            result = preproc.results()
            if result != None:
                output.update(result)

        for source, data in self.job.data().items():
            if 'errors' in data:
                self.adderror(source, str(data['errors']))

        if len(self.errors) > 0:
            output['errors'] = {}
            for k, v in self.errors.items():
                output['errors'][k] = list(v)

        return output

    def adderror(self, category, errormsg):
        """ All errors reported with this function show up in the job summary """
        if category not in self.errors:
            self.errors[category] = set()
        if isinstance(errormsg, list):
            self.errors[category].update(set(errormsg))
        else:
            self.errors[category].add(errormsg)

    def logerror(self, nodename, analyticname, error):
        """
        Store the detail of processing errors
        """
        #if analyticname != "gpu":
        logging.debug("Processing exception: %s", analyticname)
        self.adderror("node", "{0} {1} {2}".format(nodename, analyticname, error))

    def complete(self):
        """ A job is complete if archives exist for all assigned nodes and they have
            been processed sucessfullly
        """
        return self.job.nodecount == self.nodes_processed

    def good_enough(self):
        """ A job is good_enough if 95% of nodes have
            been processed sucessfullly
        """
        return self.nodes_processed >= 0.95 * float(self.job.nodecount)

    def process(self):
        """ Main entry point. All nodes are processed. """
        success = 0

        for nodename in self.job.nodenames():
            idx = self.nodes_processed
            mdata = NodeMeta(nodename, idx)

            self.mapping.populate_queries(nodename)
            try:
                logging.info("Processing node %s for job %s" % (nodename, self.job.job_id))
                self.processnode(mdata)
                self.nodes_processed += 1

            except Exception as exc:
                success -= 1
                self.adderror("node", "Exception {0} for node: {1}".format(exc, mdata.nodename))
                if self.fail_fast:
                    raise

        return success == 0

    def processnode(self, mdata):

        start, end = self.job.start_datetime.timestamp(), self.job.end_datetime.timestamp() 
        ctx = Context(start, end, self.mapping.client)

        for preproc in self.preprocs:
            ctx.mode = preproc.mode
            self.processforpreproc(ctx, mdata, preproc)

        for analytic in self.alltimestamps:
            ctx.mode = analytic.mode
            self.processforanalytic(ctx, mdata, analytic)

        for analytic in self.firstlast:
            ctx.mode = analytic.mode
            self.processfirstlast(ctx, mdata, analytic)

    def processforpreproc(self, ctx, mdata, preproc):

        preproc.hoststart(mdata.nodename)
        logging.debug("Processing %s (%s)" % (type(preproc).__name__, preproc.name))

        reqMetrics = self.mapping.getmetricstofetch(preproc.requiredMetrics)
        if False == reqMetrics:
            logging.warning("Skipping %s (%s)." % (type(preproc).__name__, preproc.name))
            preproc.hostend()
            return

        try:
            for result in ctx.fetch(reqMetrics):
                if False == self.runpreproccall(preproc, result, ctx, mdata):
                    break
        #except HTTPException as exp:
        #    # requests exception
        #    preproc.status = "failure"
        #    preproc.hostend()
        #    raise exp
        except Exception as exp:
            preproc.status = "failure"
            preproc.hostend()
            raise exp

        preproc.status = "complete"
        preproc.hostend()

    def processfirstlast(self, ctx, mdata, analytic):

        logging.debug("Processing %s (%s)" % (type(analytic).__name__, analytic.name))

        reqMetrics = self.mapping.getmetricstofetch(analytic.requiredMetrics)
        if False == reqMetrics:
            logging.warning("Skipping %s (%s)." % (type(analytic).__name__, analytic.name))
            analytic.status = "Failure"
            return

        try:
            result = next(ctx.fetch(reqMetrics))
        except StopIteration:
            analytic.status = "failure"
            return
        #except HTTPException as exp:
        #    # requests exception
        #    analytic.status = "failure"
        #    raise exp
        except Exception as exp:
            analytic.status = "failure"
            raise exp

        if False == self.runcallback(analytic, result, ctx, mdata):
            analytic.status = "failure"
            return

        try:
            print("This doesn't execute")
            result = next(ctx.fetch(reqMetrics))
        except StopIteration:
            analytic.status = "failure"
            return
        #except HTTPException as exp:
        #    # requests exception
        #    analytic.status = "failure"
        #    raise exp
        except Exception as exp:
            analytic.status = "failure"
            raise exp

        if False == self.runcallback(analytic, result, ctx, mdata):
            analytic.status = "failure"
            return

        analytic.status = "complete" 

    def processforanalytic(self, ctx, mdata, analytic):
        logging.debug("Processing %s (%s)" % (type(analytic).__name__, analytic.name))

        reqMetrics = self.mapping.getmetricstofetch(analytic.requiredMetrics)
        if False == reqMetrics:
            logging.warning("Skipping %s (%s)." % (type(analytic).__name__, analytic.name))
            analytic.status = "failure"
            return

        done = True

        while not done:
            try:
                result = next(ctx.fetch(reqMetrics))
            except StopIteration:
                done = True
            #except HTTPException as exp:
            #    # requests exception
            #    logging.warning("%s (%s) error while connecting to Prometheus %s", type(analytic).__name__, analytic.name, str(exp))
            #    analytic.status = "failure"
            #    raise exp
            except Exception as exp:
                logging.warning("%s (%s) raised exception %s", type(analytic).__name__, analytic.name, str(exp))
                analytic.status = "failure"
                raise exp

            if False == self.runcallback(analytic, result, ctx, mdata):
                done = True

        analytic.status = "complete"

    def runpreproccall(self, preproc, result, ctx, mdata):
        """ Call the pre-processor data processing function """

        retval = True
        for data, description in ctx.extractpreproc_values(result):

            if data is None and description is None:
                return False

            retval = preproc.process(ctx.timestamp, data, description)

        return retval
 
    def runcallback(self, analytic, result, ctx, mdata):
        """ Call the plugin data processing function """

        for data, description in ctx.extract_values(result):
            if data is None and description is None:
                return False

            ts = ctx.timestamp
            try:
                retval = analytic.process(mdata, ts, data, description)
                if not retval:
                    break
            except Exception as exc:
                logging.exception("%s %s @ %s", self.job.job_id, analytic.name, ts)
                self.logerror(mdata.nodename, analytic.name, str(exc))
                return False

        return False
