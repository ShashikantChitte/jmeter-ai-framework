#!/usr/bin/env python3
"""Minimal JMX builder (same XML shapes as the framework generator) used by har_to_fragment.py."""
import os
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

JMETER_VERSION = "5.6.3"


def hc(s):
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - 0x100000000 if h & 0x80000000 else h


def sp(n, v):
    return f'<stringProp name="{n}">{escape(str(v))}</stringProp>\n'


def bp(n, v):
    return f'<boolProp name="{n}">{"true" if v else "false"}</boolProp>\n'


def ip(n, v):
    return f'<intProp name="{n}">{v}</intProp>\n'


def el(testclass, gui, name, body="", children=(), enabled=True, comment=None):
    if comment:
        body = sp("TestPlan.comments", comment) + body
    attrs = (f'guiclass="{gui}" testclass="{testclass}" testname={quoteattr(name)} '
             f'enabled="{"true" if enabled else "false"}"')
    s = f"<{testclass} {attrs}>\n{body}</{testclass}>\n" if body else f"<{testclass} {attrs}/>\n"
    return s + "<hashTree>\n" + "".join(children) + "</hashTree>\n"


def http(name, method, path, body=None, form=None, children=(), comment=None):
    if body is not None:
        args = ('<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" '
                'testclass="Arguments" testname="User Defined Variables" enabled="true">\n'
                '<collectionProp name="Arguments.arguments">\n<elementProp name="" elementType="HTTPArgument">\n'
                + bp("HTTPArgument.always_encode", False) + sp("Argument.value", body)
                + sp("Argument.metadata", "=") + bp("HTTPArgument.use_equals", True)
                + '</elementProp>\n</collectionProp>\n</elementProp>\n')
        raw = bp("HTTPSampler.postBodyRaw", True)
    else:
        items = ""
        for k, v in (form or {}).items():
            items += (f'<elementProp name={quoteattr(k)} elementType="HTTPArgument">\n'
                      + bp("HTTPArgument.always_encode", True) + sp("Argument.value", v)
                      + sp("Argument.metadata", "=") + bp("HTTPArgument.use_equals", True)
                      + sp("Argument.name", k) + '</elementProp>\n')
        args = ('<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" '
                'testclass="Arguments" testname="User Defined Variables" enabled="true">\n'
                f'<collectionProp name="Arguments.arguments">\n{items}</collectionProp>\n</elementProp>\n')
        raw = ""
    b = (args + raw + sp("HTTPSampler.domain", "") + sp("HTTPSampler.port", "") + sp("HTTPSampler.protocol", "")
         + sp("HTTPSampler.contentEncoding", "UTF-8") + sp("HTTPSampler.path", path)
         + sp("HTTPSampler.method", method) + bp("HTTPSampler.follow_redirects", True)
         + bp("HTTPSampler.auto_redirects", False) + bp("HTTPSampler.use_keepalive", True)
         + bp("HTTPSampler.DO_MULTIPART_POST", False) + sp("HTTPSampler.embedded_url_re", "")
         + sp("HTTPSampler.connect_timeout", "") + sp("HTTPSampler.response_timeout", ""))
    return el("HTTPSamplerProxy", "HttpTestSampleGui", name, b, children, comment=comment)


def json_ex(name, ref, expr, match="1", default="NOT_FOUND"):
    return el("JSONPostProcessor", "JSONPostProcessorGui", name,
              sp("JSONPostProcessor.referenceNames", ref) + sp("JSONPostProcessor.jsonPathExprs", expr)
              + sp("JSONPostProcessor.match_numbers", match) + sp("JSONPostProcessor.defaultValues", default))


def regex_ex(name, ref, regex, match="1", default="NOT_FOUND"):
    return el("RegexExtractor", "RegexExtractorGui", name,
              sp("RegexExtractor.useHeaders", "false") + sp("RegexExtractor.refname", ref)
              + sp("RegexExtractor.regex", regex) + sp("RegexExtractor.template", "$1$")
              + sp("RegexExtractor.default", default) + sp("RegexExtractor.match_number", match))


def resp_assert(name, field, patterns, ttype):
    items = "".join(f'<stringProp name="{hc(p)}">{escape(p)}</stringProp>\n' for p in patterns)
    return el("ResponseAssertion", "AssertionGui", name,
              f'<collectionProp name="Asserion.test_strings">\n{items}</collectionProp>\n'
              + sp("Assertion.custom_message", "") + sp("Assertion.test_field", field)
              + bp("Assertion.assume_success", False) + ip("Assertion.test_type", ttype))


def code_ok(name="Assert - HTTP 2xx"):
    return resp_assert(name, "Assertion.response_code", ["^20[0-9]$"], 1)


def think_timer(name="Think Time (uniform random)"):
    return el("UniformRandomTimer", "UniformRandomTimerGui", name,
              sp("ConstantTimer.delay", "${__P(think_min_ms,1000)}")
              + sp("RandomTimer.range", "${__P(think_range_ms,3000)}"))


def tc(name, children):
    return el("TransactionController", "TransactionControllerGui", name,
              bp("TransactionController.parent", True) + bp("TransactionController.includeTimers", False), children)


def fragment(name, children):
    return el("TestFragmentController", "TestFragmentControllerGui", name, "", children)


def write_fragment_file(path, title, comment, children):
    inner = el("TestPlan", "TestPlanGui", f"{title} (fragment file)",
               sp("TestPlan.comments", comment) + bp("TestPlan.functional_mode", False)
               + bp("TestPlan.tearDown_on_shutdown", True) + bp("TestPlan.serialize_threadgroups", False)
               + '<elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" '
                 'testclass="Arguments" testname="User Defined Variables" enabled="true">\n'
                 '<collectionProp name="Arguments.arguments"/>\n</elementProp>\n' + sp("TestPlan.user_define_classpath", ""),
               [fragment(title, children)])
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<jmeterTestPlan version="1.2" properties="5.0" jmeter="{JMETER_VERSION}">\n'
           f'<hashTree>\n{inner}</hashTree>\n</jmeterTestPlan>\n')
    root = ET.fromstring(xml.encode("utf-8"))
    ET.indent(root, space="  ")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n")
