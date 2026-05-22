# Fix for “model_providers contains reserved built-in provider IDs: `openai`”

## Error
`invalid configuration: model_providers contains reserved built-in provider IDs: \`openai\`. Built-in providers cannot be overridden. Rename your custom provider (for example, \`openai-custom\`).`

## File
`C:\Users\manve\.codex\config.toml`

## Change
Rename the custom model provider key from `openai` to a non-reserved name (e.g. `openai-custom`).

### 1) Update:
```toml
model_provider = "openai"
```
to:
```toml
model_provider = "openai-custom"
```

### 2) Update provider table header:
```toml
[model_providers.openai]
```
to:
```toml
[model_providers.openai-custom]
```

## Apply
Restart the chat after saving.

## Your current snippet (edit these parts)
```toml
model = "gpt-4o"
model_provider = "openai-custom"

[model_providers.openai-custom]
name = "OpenAI"
base_url = "https://api.openai.com/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
