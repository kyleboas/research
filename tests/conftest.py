import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _NoopMessages:
    def create(self, **_kwargs):
        return SimpleNamespace(content=[])


class _NoopAnthropic:
    def __init__(self, *args, **kwargs):
        self.messages = _NoopMessages()


class _NoopEmbeddings:
    def create(self, **_kwargs):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[])])


class _NoopOpenAI:
    def __init__(self, *args, **kwargs):
        self.embeddings = _NoopEmbeddings()


sys.modules.setdefault("anthropic", SimpleNamespace(Anthropic=_NoopAnthropic))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=_NoopOpenAI))
