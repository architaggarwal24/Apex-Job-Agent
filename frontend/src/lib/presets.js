// frontend/src/lib/presets.js
//
// One-click starting points for the "Add a provider" form. Anything that speaks
// the OpenAI chat-completions protocol works. Only the CodeCraft URL was taken
// from the provider's own docs; the rest are the providers' well-known public
// endpoints written from memory — they pre-fill an editable field, and "Test"
// will tell you straight away if one has moved.
export const PRESETS = [
  { id: 'codecraft', label: 'CodeCraft API', base_url: 'https://codecraftapi.com/v1',
    hint: 'Keys start with cc_. Model ids come from the provider, so use “Fetch models” rather than guessing.' },
  { id: 'groq',      label: 'Groq',          base_url: 'https://api.groq.com/openai/v1' },
  { id: 'together',  label: 'Together AI',   base_url: 'https://api.together.xyz/v1' },
  { id: 'deepseek',  label: 'DeepSeek',      base_url: 'https://api.deepseek.com/v1' },
  { id: 'xai',       label: 'xAI',           base_url: 'https://api.x.ai/v1' },
  { id: 'cerebras',  label: 'Cerebras',      base_url: 'https://api.cerebras.ai/v1' },
  { id: 'fireworks', label: 'Fireworks AI',  base_url: 'https://api.fireworks.ai/inference/v1' },
  { id: 'lmstudio',  label: 'LM Studio (local)', base_url: 'http://localhost:1234/v1',
    hint: 'No key needed. Start the local server inside LM Studio first.' },
  { id: 'custom',    label: 'Other — any OpenAI-compatible URL', base_url: '' },
]
