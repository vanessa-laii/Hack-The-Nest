# Prompts

Every prompt for a text-model call lives here as `<prompt_name>.md` and is loaded by
`app/llm.py:complete_json(prompt_name, ...)`. Variables are filled with `{name}` placeholders;
web text must be passed through `wrap_untrusted()` first (TR-9). No prompts inside Python code.
