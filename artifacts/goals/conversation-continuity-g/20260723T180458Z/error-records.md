# Error records

1. Initial `0031` upgrade failed transactionally because SQLAlchemy parsed a
   colon-prefixed SQL literal fragment as a bind parameter. No partial schema
   remained. The SQL was made unambiguous and upgrade/downgrade/reapply passed.
2. The first full integration run found stale head assertions, a generic
   control-plane path into the dedicated wait, and an unscoped test query.
   These were corrected. Tool Gateway failures in that run were cascading
   claims of a pending fixture left by the earlier failed assertion; a fresh
   database passed the complete suite.
3. A later verifier attempt caught an indentation error before test
   collection. Static compilation and Ruff were added before the next full
   isolated run.
