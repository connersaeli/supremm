from abc import ABC, abstractmethod


class Summarize(ABC):
    """ Abstract base class of the job summarization interface """

    def __init__(self, preprocessors, analytics, job, config, fail_fast=False):
        self.preprocs = preprocessors
        self.alltimestamps = [x for x in analytics if x.mode in ("all", "timeseries")]
        self.firstlast = [x for x in analytics if x.mode == "firstlast"]
        self.errors = {}
        self.job = job
        self.fail_fast = fail_fast

    @abstractmethod
    def get(self):
        """ Return a dict with the summary information """
        pass

    def adderror(self, category, errormsg):
        """ All errors reported with this function show up in the job summary """
        if category not in self.errors:
            self.errors[category] = set()
        if isinstance(errormsg, list):
            self.errors[category].update(set(errormsg))
        else:
            self.errors[category].add(errormsg)

    @abstractmethod
    def process(self):
        """ Main entry point. All of a job's nodes are processed """
        pass

    @abstractmethod
    def complete(self):
        """ A job is complete if archives exist for all assigned nodes and they have
            been processed sucessfullly
        """
        pass

    @abstractmethod
    def good_enough(self):
        """ A job is good_enough if archives for 95% of nodes have
            been processed sucessfullly
        """
        pass
