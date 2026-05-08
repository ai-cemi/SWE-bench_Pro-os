#!/bin/bash
# Tests that special characters in test IDs (*, \n, brackets) survive the full
# pipeline: CSV arg -> run_script.sh IFS split -> pytest invocation.
#
# Usage: bash dataset_preprocessing/test_glob_expansion.sh

TESTS_CSV='test/units/modules/test_mount_facts.py::test_list_mounts[gen_aix_filesystems_entries-*\n*,test/units/modules/test_mount_facts.py::test_invocation,test/units/modules/test_mount_facts.py::test_get_partition_uuid'

EXPECTED_COUNT=3
FAIL=0

run_selected_tests() {
  local test_files=("$@")
  for t in "${test_files[@]}"; do
    echo "$t"
  done
}

if [[ "$TESTS_CSV" == *","* ]]; then
  IFS=',' read -r -a TEST_FILES <<< "$TESTS_CSV"
else
  TEST_FILES=("$TESTS_CSV")
fi

mapfile -t RESOLVED < <(run_selected_tests "${TEST_FILES[@]}")

echo "=== Resolved test IDs (${#RESOLVED[@]}) ==="
for t in "${RESOLVED[@]}"; do
  echo "  $t"
done
echo ""

# Check count
if [[ "${#RESOLVED[@]}" -eq "$EXPECTED_COUNT" ]]; then
  echo "PASS: got $EXPECTED_COUNT test IDs"
else
  echo "FAIL: expected $EXPECTED_COUNT, got ${#RESOLVED[@]}"
  FAIL=$((FAIL + 1))
fi

# Check the asterisk-containing ID survived intact
STAR_ID='test/units/modules/test_mount_facts.py::test_list_mounts[gen_aix_filesystems_entries-*\n*'
FOUND=0
for t in "${RESOLVED[@]}"; do
  if [ "$t" = "$STAR_ID" ]; then
    FOUND=1
    break
  fi
done
if [[ "$FOUND" -eq 1 ]]; then
  echo "PASS: asterisk-containing ID passed through unexpanded"
else
  echo "FAIL: asterisk-containing ID was lost or expanded"
  FAIL=$((FAIL + 1))
fi

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  echo "All tests passed."
else
  echo "$FAIL test(s) failed."
  exit 1
fi
