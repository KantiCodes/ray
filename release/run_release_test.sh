#!/bin/bash

set -ex

cd "${0%/*}" || exit 1

reason() {
  # Keep in sync with e2e.py ExitCode enum
  if [ "$1" -eq 0 ]; then
    REASON="success"
  elif [ "$1" -gt 1 ] && [ "$1" -le 10 ]; then
    REASON="runtime error"
  elif [ "$1" -gt 10 ] && [ "$1" -le 20 ]; then
    REASON="infra error"
  elif [ "$1" -gt 30 ] && [ "$1" -le 40 ]; then
    REASON="infra timeout"
  elif [ "$1" -eq 42 ]; then
    REASON="command timeout"
  elif [ "$1" -gt 40 ] && [ "$1" -le 50 ]; then
    REASON="command error"
  fi
  echo "${REASON}"
}

while [[ $# -gt 0 ]]
do
key="$1"
case $key in
    --ray-test-repo)
    shift
    RAY_TEST_REPO=$1
    ;;
    --ray-test-branch)
    shift
    RAY_TEST_BRANCH=$1
    ;;
    --release-results-dir)
    shift
    RELEASE_RESULTS_DIR=$1
    ;;
    *)
    break
esac
shift
done

RAY_TEST_REPO=${RAY_TEST_REPO-https://github.com/ray-project/ray.git}
RAY_TEST_BRANCH=${RAY_TEST_BRANCH-master}
RELEASE_RESULTS_DIR=${RELEASE_RESULTS_DIR-/tmp/artifacts}

export RAY_TEST_REPO RAY_TEST_BRANCH RELEASE_RESULTS_DIR

pip uninstall -q -y ray
pip install -q -r requirements.txt
pip install -q -U boto3 botocore
TMPDIR=$(mktemp -d -t release-XXXXXXXXXX)
git clone -b "${RAY_TEST_BRANCH}" "${RAY_TEST_REPO}" "${TMPDIR}"

pushd "${TMPDIR}" || true

RETRY_NUM=0
MAX_RETRIES=${MAX_RETRIES-3}

if [ "${BUILDKITE_RETRY_COUNT-0}" -ge 1 ]; then
  echo "This is a manually triggered retry from the Buildkite web UI, so we set the number of infra retries to 1."
  MAX_RETRIES=1
fi

ALL_EXIT_CODES=()
while [ "$RETRY_NUM" -lt "$MAX_RETRIES" ]; do
  RETRY_NUM=$((RETRY_NUM + 1))

  if [ "$RETRY_NUM" -gt 1 ]; then
    # Sleep for random time between 30 and 90 minutes
    SLEEP_TIME=$((1800 + RANDOM % 5400))
    echo "----------------------------------------"
    echo "Retry count: ${RETRY_NUM}/${MAX_RETRIES}. Sleeping for ${SLEEP_TIME} seconds before retrying the run."
    echo "----------------------------------------"
    sleep ${SLEEP_TIME}
  fi

  sudo rm -rf "${RELEASE_RESULTS_DIR}"/* || true

  python ray_release/scripts/run_release_test.py "$@"
  EXIT_CODE=$?
  REASON=$(reason "${EXIT_CODE}")
  ALL_EXIT_CODES[${#ALL_EXIT_CODES[@]}]=$EXIT_CODE

  case ${EXIT_CODE} in
    0)
    echo "Script finished successfully on try ${RETRY_NUM}/${MAX_RETRIES}"
    break
    ;;
    7 | 9 | 10)
    echo "Script failed on try ${RETRY_NUM}/${MAX_RETRIES} with exit code ${EXIT_CODE} (${REASON})."
    ;;
    *)
    echo "Script failed on try ${RETRY_NUM}/${MAX_RETRIES} with exit code ${EXIT_CODE} (${REASON}), aborting."
    break
    ;;
  esac

done

sudo rm -rf /tmp/ray_release_test_artifacts/* || true
sudo cp -rf "${RELEASE_RESULTS_DIR}"/* /tmp/ray_release_test_artifacts/ || true

echo "----------------------------------------"
echo "release test finished with final exit code ${EXIT_CODE} after ${RETRY_NUM}/${MAX_RETRIES} tries"
echo "Run results:"

COUNTER=1
for EX in "${ALL_EXIT_CODES[@]}"; do
  REASON=$(reason "${EX}")
  echo "  Run $COUNTER: Exit code = ${EX} (${REASON})"
  COUNTER=$((COUNTER + 1))
done

echo "----------------------------------------"

REASON=$(reason "${EXIT_CODE}")
echo "Final release test exit code is ${EXIT_CODE} (${REASON})"

if [ "$EXIT_CODE" -gt 30 ] && [ "$EXIT_CODE" -le 40 ]; then
  echo "RELEASE MANAGER: This is likely an infra error that can be solved by RESTARTING this test."
else
  echo "RELEASE MANAGER: This could be an error in the test. Please REVIEW THE LOGS and ping the test owner."
fi

popd || true
rm -rf "${TMPDIR}" || true

exit $EXIT_CODE
