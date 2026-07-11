# Default Test Data - DO NOT MODIFY

This directory contains the bedrock test data files used by SpeakesQuery's YAML-driven test framework (141+ tests). These files are the deterministic source of truth for all query validation and regression testing.

## Contents

- `output_parquets/test0.parquet` - 5 rows (level, message, errorCode, x, userRole)
- `output_parquets/test1.parquet` - 3 rows (similar schema)
- `error_tracking/system_alerts.parquet` - 100 rows (region, userRole, attempts, etc.)

## Immutability

This directory and all of its contents are set to **read-only and immutable** (`chmod a-w` + `chflags uchg` on macOS). This is intentional.

**Do not:**
- Edit, rename, or delete any file in this directory
- Add new files here (create a separate test index instead)
- Remove the immutable flags

**If you need to modify test data:**
1. Create a new parquet file in a separate index directory
2. Add new YAML test cases pointing to your new file
3. Leave these originals untouched

## Referenced by

- All YAML test files in `tests/yaml/` (tiers 1-4)
- `macros/first_test_macro.yaml`
- `lookups/test.csv` (companion lookup file, stored in `lookups/` for resolver compatibility)
