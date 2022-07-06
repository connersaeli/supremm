#!/usr/bin/env python3
""" Timeseries generator module """

from supremm.plugin import RateConvertingTimeseriesPlugin
import numpy

class LustreTimeseries(RateConvertingTimeseriesPlugin):
    """ Generate the Lustre usage as a timeseries data """

    name = property(lambda x: "lnet")
    requiredMetrics = property(lambda x: ["lustre.llite.read_bytes.total", "lustre.llite.write_bytes.total"])
    optionalMetrics = property(lambda x: [])
    derivedMetrics = property(lambda x: [])

    def __init__(self, job):
        super(LustreTimeseries, self).__init__(job)

    def computetimepoint(self, data):
        return (numpy.sum(data[0]) + numpy.sum(data[1])) / 1048576.0
