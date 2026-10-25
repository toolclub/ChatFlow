"""Run tests with deterministic dummy configuration, without loading deployment secrets.

Usage: python tests/run_regression.py -q [tests/...]
External DB integration is opt-in through CHATFLOW_TEST_DATABASE_URL and
CHATFLOW_TEST_REDIS_URL; see test_db_recovery_integration.py.
"""
import ast
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Fill all settings (including optional credentials) so .env cannot supply them.
module = ast.parse((ROOT / 'config.py').read_text())
settings = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'Settings')
import json
for field in settings.body:
    if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
        continue
    key = field.target.id.upper()
    try:
        value = ast.literal_eval(field.value) if field.value else None
    except (ValueError, TypeError):
        value = None
    if value is None:
        annotation = ast.unparse(field.annotation)
        value = False if annotation == 'bool' else 1 if annotation in {'int', 'float'} else {} if annotation.startswith('dict') else [] if annotation.startswith('list') else 'test'
    os.environ[key] = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
os.environ.update({
    'LLM_BASE_URL': 'http://127.0.0.1:9/v1', 'EMBEDDING_BASE_URL': 'http://127.0.0.1:9/v1',
    'API_KEY': 'test-only', 'QDRANT_URL': 'http://127.0.0.1:9',
    'DATABASE_URL': 'postgresql+asyncpg://test:test@127.0.0.1:9/chatflow_test',
    'REDIS_URL': 'redis://127.0.0.1:9/15', 'BACKEND_HOST': '127.0.0.1', 'BACKEND_PORT': '8000',
    'CONVERSATIONS_DIR': '/tmp/chatflow-test-conversations', 'LOG_DIR': '/tmp/chatflow-test-logs',
    'LONGTERM_MEMORY_ENABLED': 'false', 'FACT_EXTRACTION_ENABLED': 'false',
    'SEMANTIC_CACHE_ENABLED': 'false', 'SANDBOX_ENABLED': 'false',
    'QUANT_WARMER_ENABLED': 'false', 'QUANT_SCREENING_ENABLED': 'false',
    'JWT_SECRET_KEY': 'local-test-key-not-for-deployment',
})
if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:] or ['-q', 'tests']))
