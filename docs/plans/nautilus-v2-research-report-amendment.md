# Bounded report correction within the V2 research journey

This implementation note supplements the clean approved plan without changing its
recorded hash or engine/scope decision. The root's actual baseline browser journey
on October6 found `ReferenceError: save is not defined` when opening the full report.
The publicly downloaded HTML contains `<body onload="save()">` and no definition of
that function. Accounting content and tables render, but the report logs a reachable
script error on the supported research path.

T4's report ownership additionally includes
`backend/src/msai/services/report_generator.py` and its existing owning
`backend/tests/unit/test_report_generator.py`. The producer must first reproduce the
undefined load handler with an owning RED test, then make the smallest correction
for newly generated reports. Keep report delivery/authentication, rendered financial
meaning and unrelated working scripts intact. Saved baseline report bytes are retained
unchanged, and their existing error stays labeled as baseline evidence.

This adds no dependency upgrade, broad HTML sanitizer, new report framework or live
scope. Final candidate reviews and actual new-report browser acceptance must cover
the correction; existing clean plan evidence is not final certification.
