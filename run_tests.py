"""
ATrades Regression Test Runner
================================
Usage:
  python run_tests.py              # run all tests
  python run_tests.py -k config    # run only config tests
  python run_tests.py -k "e2e"     # run only E2E tests
  python run_tests.py --cov        # run with coverage report
  python run_tests.py -v           # verbose output

Available test sections (-k filter):
  TestConfig          - Config & environment loading
  TestUniverse        - Stock universe / fallback symbols
  TestStore           - SQLite persistence layer
  TestBreakoutSignals - Signal detection pipeline
  TestBreakoutScorer  - Scoring engine
  TestMarketRegime    - Market regime classification
  TestTradeSetup      - Trade setup calculation
  TestRiskManager     - Risk approval logic
  TestWebappAPI       - Webapp REST API endpoints
  TestE2EPipeline     - Full end-to-end pipeline
"""
import subprocess
import sys

if __name__ == "__main__":
    args = sys.argv[1:]

    cmd = [sys.executable, "-m", "pytest", "tests/", "--tb=short"]

    if "--cov" in args:
        args.remove("--cov")
        cmd += ["--cov=.", "--cov-report=term-missing", "--cov-omit=tests/*,run_tests.py"]

    if not any(a.startswith("-v") or a == "--verbose" for a in args):
        cmd.append("-v")

    cmd += args
    sys.exit(subprocess.call(cmd))
