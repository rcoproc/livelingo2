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

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

#GROQ_URL = "http://192.168.137.1:4000/v1/chat/completions"

# Friendly names for the most common language codes (used in the prompt).
_LANG_NAMES = {
    "fr": "French", "en": "English", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "pl": "Polish", "tr": "Turkish", "hi": "Hindi",
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


class LLMTranslator:
    def __init__(self, config):
        self.cfg = config
        self.api_key = config.GROQ_API_KEY
        self.model = config.GROQ_MODEL
        self.timeout = config.LLM_TIMEOUT

        src = _lang_name(config.SOURCE_LANG)
        tgt = _lang_name(config.TARGET_LANG)
        self.system_prompt = (
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

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    def translate(self, text):
        """Clean + translate `text`. Returns the target-language string."""
        text = (text or "").strip()
        if not text:
            return ""

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
        }
        try:
            resp = requests.post(
                GROQ_URL, headers=self._headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        # Turn common HTTP errors into clear, actionable messages.
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
            resp = requests.post(
                GROQ_URL, headers=self._headers(), json=payload, timeout=self.timeout
            )
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
    def generate_meeting_summary(self, transcript_text):
        """Generate a structured Markdown summary from a transcript."""
        transcript_text = (transcript_text or "").strip()
        if not transcript_text:
            return ""

        system_prompt = (
            "Você é um assistente executivo sênior. O usuário fornecerá a transcrição crua de uma conversa ou reunião.\n"
            "Sua tarefa é analisar o conteúdo falado e gerar um resumo executivo em Português do Brasil.\n"
            "A resposta DEVE obrigatoriamente seguir a estrutura Markdown abaixo:\n\n"
            "## 📌 Assunto Principal\n"
            "[Identifique o tema central da conversa]\n\n"
            "## 📝 Resumo Objetivo\n"
            "[Um parágrafo resumindo o que foi discutido de forma concisa e direta]\n\n"
            "## ✅ Tarefas e Ações\n"
            "- [Liste tarefas a serem executadas, pontos de ação ou itens que precisam de análise futura de acordo com o contexto.]\n"
            "- [Se não houver tarefas óbvias, deduza possíveis próximos passos baseados na conversa.]\n"
        )

        payload = {
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 1000,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Transcrição da conversa:\n{transcript_text}"},
            ],
        }
        try:
            resp = requests.post(
                GROQ_URL, headers=self._headers(), json=payload, timeout=180.0
            )
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
