"""
llm.py
======
High-quality translation via a free LLM (Groq's OpenAI-compatible API).

Instead of a literal machine translation, an instruction-tuned LLM takes the
*raw* (often imperfect) speech-to-text transcription and produces a clean,
natural, fluent translation in one step — fixing recognition glitches,
punctuation and filler words along the way. This is what makes tools like
Typeless feel so polished.

Get a free API key (no credit card) at: https://console.groq.com/keys

Drop-in compatible with translate.Translator: exposes `.translate(text)`.
Uses `requests` (already installed as a dependency of deep-translator).
"""

import json
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# GROQ_URL = "http://192.168.137.1:4000/v1/chat/completions"

# Friendly names for the most common language codes (used in the prompt).
_LANG_NAMES = {
    "fr": "French",
    "en": "English",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "pl": "Polish",
    "tr": "Turkish",
    "hi": "Hindi",
}

GROQ_KEY_HELP = """\
No Groq API key is set. It's free (no credit card):
  1. Go to https://console.groq.com/keys  and sign up / log in.
  2. Click "Create API Key", copy it.
  3. Paste it into your .env file:   GROQ_API_KEY=gsk_xxxxxxxx
Then run the tool again. (Or set TRANSLATION_ENGINE=google to use Google instead.)
"""


def _lang_name(code):
    return _LANG_NAMES.get((code or "").lower(), code)


class LLMError(Exception):
    """Raised when the LLM translation request fails."""


def _translation_prompt(config):
    src = _lang_name(config.SOURCE_LANG)
    tgt = _lang_name(config.TARGET_LANG)
    if getattr(config, "LOW_LATENCY", False):
        return (
            f"Translate {src} speech-to-text to natural {tgt}. "
            f"Fix minor STT errors. Output ONLY the {tgt} translation."
        )
    return (
        f"You are a professional real-time interpreter. You receive a raw "
        f"speech-to-text transcription in {src}. It may contain recognition "
        f"errors, missing punctuation, filler words, or be only a fragment.\n"
        f"Produce a clean, natural, fluent {tgt} translation of what the "
        f"speaker most likely meant.\n"
        f"Rules:\n"
        f"- Output ONLY the {tgt} translation. No quotes, no notes, no "
        f"explanations, no preamble.\n"
        f"- Silently fix obvious transcription errors and punctuation so it "
        f"reads naturally in {tgt}.\n"
        f"- Preserve meaning and tone; never add information.\n"
        f"- If the input is empty, gibberish, or not real speech, output "
        f"nothing at all."
    )


class LLMTranslator:
    def __init__(self, config):
        self.cfg = config
        self.api_key = config.GROQ_API_KEY
        self.model = config.GROQ_MODEL
        self.timeout = config.LLM_TIMEOUT
        self.system_prompt = _translation_prompt(config)
        self.session = requests.Session()
        self.session.headers.update(self._headers())

    def refresh_prompt(self):
        """Rebuild system prompt after SOURCE/TARGET swap ([g])."""
        self.system_prompt = _translation_prompt(self.cfg)

    def set_language_pair(self, source=None, target=None):
        """Alias for swap rebind — languages live on self.cfg."""
        if source is not None:
            self.cfg.SOURCE_LANG = source
        if target is not None:
            self.cfg.TARGET_LANG = target
        self.refresh_prompt()

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _max_tokens_for(self, text):
        if getattr(self.cfg, "LOW_LATENCY", False):
            return min(150, max(32, len(text) * 3))
        return 400

    def _translation_payload(self, text):
        return {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": self._max_tokens_for(text),
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
        }

    def _parse_stream_line(self, line):
        if not line.startswith("data: "):
            return None
        data = line[6:].strip()
        if not data or data == "[DONE]":
            return None
        try:
            chunk = json.loads(data)
            return chunk["choices"][0]["delta"].get("content") or ""
        except (ValueError, KeyError, IndexError):
            return None

    def _raise_for_status(self, resp):
        if resp.status_code == 401:
            raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if resp.status_code == 404:
            raise LLMError(
                f"Groq model '{self.model}' not found (404). "
                f"Set a valid GROQ_MODEL (e.g. llama-3.1-8b-instant)."
            )
        if resp.status_code == 429:
            raise LLMError(
                "Groq rate limit reached (429). Wait a moment or use a smaller "
                "model (GROQ_MODEL=llama-3.1-8b-instant)."
            )
        if resp.status_code >= 400:
            raise LLMError(f"Groq error {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------ #
    def translate_stream(self, text, on_token=None):
        """Translate with Groq streaming; calls on_token(partial_text) per delta."""
        text = (text or "").strip()
        if not text:
            return ""

        payload = self._translation_payload(text)
        payload["stream"] = True

        try:
            resp = self.session.post(
                GROQ_URL, json=payload, timeout=self.timeout, stream=True
            )
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        self._raise_for_status(resp)

        parts = []
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            delta = self._parse_stream_line(raw)
            if not delta:
                continue
            parts.append(delta)
            if on_token:
                on_token("".join(parts))

        return "".join(parts).strip().strip('"').strip()

    # ------------------------------------------------------------------ #
    def translate(self, text):
        """Clean + translate `text`. Returns the target-language string."""
        text = (text or "").strip()
        if not text:
            return ""

        try:
            resp = self.session.post(
                GROQ_URL, json=self._translation_payload(text), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        self._raise_for_status(resp)

        try:
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Groq response: {exc}") from exc

        # Strip stray surrounding quotes the model might add.
        return out.strip().strip('"').strip()

    # ------------------------------------------------------------------ #
    def explain_synonyms(self, word):
        """Explain the meaning of `word` in Portuguese and provide English examples with synonyms."""
        word = (word or "").strip()
        if not word:
            return ""

        system_prompt = (
            "Você é um professor de inglês nativo e experiente. O usuário fornecerá uma palavra em inglês.\n"
            "Sua tarefa é explicar essa palavra em português e fornecer de 2 a 3 exemplos práticos de frases utilizando seus sinônimos.\n"
            "Regra fundamental: As frases de exemplo originais DEVEM ser escritas em INGLÊS, e suas respectivas traduções DEVEM ser escritas em PORTUGUÊS.\n\n"
            "Siga RIGOROSAMENTE o formato estrutural do exemplo abaixo (para a palavra 'Fast'):\n\n"
            "1. **Significado e Uso**:\n"
            "A palavra 'fast' é usada para descrever algo que se move ou acontece em alta velocidade.\n\n"
            "2. **Sinônimos Comuns em Inglês**:\n"
            "- Quick (Rápido)\n"
            "- Rapid (Rápido/Acelerado)\n"
            "- Swift (Veloz/Ágil)\n\n"
            "3. **Exemplos Práticos com Sinônimos**:\n"
            "- **Frase em Inglês**: She gave a *quick* response to my question.\n"
            "  **Tradução**: Ela deu uma resposta rápida à minha pergunta.\n\n"
            "- **Frase em Inglês**: The company experienced *rapid* growth this year.\n"
            "  **Tradução**: A empresa experimentou um crescimento rápido este ano.\n\n"
            "Atenção: Escreva as frases de exemplo originais estritamente em INGLÊS, e apenas a linha de 'Tradução' correspondente em PORTUGUÊS. Nunca traduza a frase original do exemplo para o português na linha 'Frase em Inglês'."
        )

        payload = {
            "model": self.model,
            "temperature": 0.4,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Palavra: {word}"},
            ],
        }
        try:
            resp = self.session.post(GROQ_URL, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        if resp.status_code == 401:
            raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if resp.status_code >= 400:
            raise LLMError(f"Groq error {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Groq response: {exc}") from exc

        return out.strip()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token count (safe overestimate for mixed EN/PT)."""
        # ~3 chars/token for Romance languages is a common overestimate.
        n = len(text or "")
        return max(1, (n + 2) // 3)

    def _summary_input_budget(self) -> int:
        """
        Max transcript tokens for one Groq request.

        Free-tier 8b-instant often caps ~6000 TPM *per request*. Leave room
        for system prompt (~400) + max_tokens output (800) + margin.
        Override: SUMMARY_MAX_INPUT_TOKENS in config.
        """
        try:
            override = int(getattr(self.cfg, "SUMMARY_MAX_INPUT_TOKENS", 0) or 0)
        except (TypeError, ValueError):
            override = 0
        if override > 500:
            return override
        # Default conservative budget works for llama-3.1-8b-instant free tier.
        return 4000

    def _chat_completion(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 800,
        timeout: float = 180.0,
    ) -> str:
        """Single non-stream chat call; raises LLMError on failure."""
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = self.session.post(GROQ_URL, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        if resp.status_code == 401:
            raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if resp.status_code == 413 or (
            resp.status_code == 429 and "too large" in (resp.text or "").lower()
        ):
            raise LLMError(
                f"Groq request too large ({resp.status_code}): transcript "
                f"exceeds model/TPM limit. LiveLingo will chunk automatically; "
                f"if this persists, set SUMMARY_MAX_INPUT_TOKENS lower or "
                f"GROQ_MODEL to a higher-tier model. Detail: {resp.text[:180]}"
            )
        if resp.status_code >= 400:
            raise LLMError(f"Groq error {resp.status_code}: {resp.text[:280]}")

        try:
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Groq response: {exc}") from exc
        return (out or "").strip()

    def _split_transcript_chunks(self, transcript_text: str, budget: int) -> list:
        """
        Split transcript into pieces under `budget` tokens, preferring
        line boundaries (each line is usually one phrase).
        """
        text = (transcript_text or "").strip()
        if not text:
            return []
        if self._estimate_tokens(text) <= budget:
            return [text]

        lines = text.split("\n")
        chunks = []
        cur: list = []
        cur_tok = 0
        for line in lines:
            line_tok = self._estimate_tokens(line) + 1
            # Single huge line — hard-cut by characters
            if line_tok > budget:
                if cur:
                    chunks.append("\n".join(cur))
                    cur, cur_tok = [], 0
                # ~3 chars/token
                step = max(200, budget * 3)
                for i in range(0, len(line), step):
                    chunks.append(line[i : i + step])
                continue
            if cur and cur_tok + line_tok > budget:
                chunks.append("\n".join(cur))
                cur, cur_tok = [], 0
            cur.append(line)
            cur_tok += line_tok
        if cur:
            chunks.append("\n".join(cur))
        return chunks or [text[: budget * 3]]

    def generate_meeting_summary(self, transcript_text):
        """
        Structured Markdown executive summary from a transcript.

        Long sessions are map-reduced so free-tier Groq TPM limits
        (e.g. 6000 on llama-3.1-8b-instant) are not exceeded.
        """
        transcript_text = (transcript_text or "").strip()
        if not transcript_text:
            return ""

        final_system = (
            "Você é um assistente executivo sênior. O usuário fornecerá a "
            "transcrição crua de uma conversa ou reunião.\n"
            "Sua tarefa é analisar o conteúdo falado e gerar um resumo "
            "executivo em Português do Brasil.\n"
            "A resposta DEVE obrigatoriamente seguir a estrutura Markdown "
            "abaixo:\n\n"
            "## 📌 Assunto Principal\n"
            "[Identifique o tema central da conversa]\n\n"
            "## 📝 Resumo Objetivo\n"
            "[Um parágrafo resumindo o que foi discutido de forma concisa "
            "e direta]\n\n"
            "## ✅ Tarefas e Ações\n"
            "- [Liste tarefas a serem executadas, pontos de ação ou itens "
            "que precisam de análise futura de acordo com o contexto.]\n"
            "- [Se não houver tarefas óbvias, deduza possíveis próximos "
            "passos baseados na conversa.]\n"
        )
        partial_system = (
            "Você resume trechos de uma reunião em Português do Brasil. "
            "Seja denso e fiel ao texto. Estrutura:\n"
            "### Trecho\n"
            "- Assunto: …\n"
            "- Pontos: …\n"
            "- Ações: …\n"
            "Sem introdução fora dessa estrutura."
        )
        merge_system = (
            "Você é um assistente executivo sênior. Abaixo há resumos "
            "parciais de uma mesma reunião (a transcrição foi dividida por "
            "limite de tokens da API).\n"
            "Una tudo num único resumo executivo em Português do Brasil, "
            "sem repetir, seguindo EXATAMENTE:\n\n"
            "## 📌 Assunto Principal\n\n"
            "## 📝 Resumo Objetivo\n\n"
            "## ✅ Tarefas e Ações\n"
        )

        budget = self._summary_input_budget()
        pieces = self._split_transcript_chunks(transcript_text, budget)

        # Fast path: fits in one request
        if len(pieces) == 1:
            try:
                return self._chat_completion(
                    system=final_system,
                    user=f"Transcrição da conversa:\n{pieces[0]}",
                    temperature=0.3,
                    max_tokens=1000,
                )
            except LLMError as exc:
                # If still too large (bad estimate), force smaller budget once
                msg = str(exc).lower()
                if "too large" not in msg and "413" not in msg:
                    raise
                pieces = self._split_transcript_chunks(
                    transcript_text, max(1500, budget // 2)
                )

        # Map: partial summaries (pace calls — free-tier ~6k TPM/min)
        partials = []
        n = len(pieces)
        tpm_cap = 5500.0  # stay under common free-tier 6000 TPM
        last_call = 0.0
        for i, piece in enumerate(pieces, 1):
            est = self._estimate_tokens(piece) + 600  # +reply headroom
            if last_call > 0:
                need = 60.0 * est / tpm_cap
                waited = time.monotonic() - last_call
                if waited < need:
                    pause = min(25.0, need - waited)
                    try:
                        from . import ui as _ui

                        _ui.dim(
                            f"Resumo IA: aguardando {pause:.0f}s (limite TPM Groq)…"
                        )
                    except Exception:
                        pass
                    time.sleep(pause)
            try:
                from . import ui as _ui

                _ui.dim(
                    f"Resumo IA: parte {i}/{n} (~{self._estimate_tokens(piece)} tok)…"
                )
            except Exception:
                pass
            part = self._chat_completion(
                system=partial_system,
                user=f"Trecho {i}/{n} da transcrição:\n{piece}",
                temperature=0.2,
                max_tokens=500,
            )
            last_call = time.monotonic()
            if part:
                partials.append(f"### Parte {i}/{n}\n{part}")

        if not partials:
            raise LLMError("Groq returned empty partial summaries.")

        merged_src = "\n\n".join(partials)
        # If merge payload still huge, keep only the partials (already useful)
        if self._estimate_tokens(merged_src) > budget:
            # Truncate oldest partials until under budget
            kept = []
            tok = 0
            for p in reversed(partials):
                pt = self._estimate_tokens(p)
                if kept and tok + pt > budget:
                    break
                kept.append(p)
                tok += pt
            merged_src = "\n\n".join(reversed(kept))

        # Pace before final merge too
        est_m = self._estimate_tokens(merged_src) + 1000
        if last_call > 0:
            need = 60.0 * est_m / tpm_cap
            waited = time.monotonic() - last_call
            if waited < need:
                pause = min(25.0, need - waited)
                try:
                    from . import ui as _ui

                    _ui.dim(f"Resumo IA: aguardando {pause:.0f}s antes do merge…")
                except Exception:
                    pass
                time.sleep(pause)

        try:
            from . import ui as _ui

            _ui.dim("Resumo IA: unindo partes no resumo executivo final…")
        except Exception:
            pass

        return self._chat_completion(
            system=merge_system,
            user=f"Resumos parciais da reunião:\n\n{merged_src}",
            temperature=0.3,
            max_tokens=1000,
        )
