{
  "name": "zen-crm-node",
  "private": true,
  "version": "1.0.0",
  "engines": {
    "node": "{{ node_deps.engines_node | default('>=18') }}"
  },
  "scripts": {
    "playwright:install": "PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-.ms-playwright} npx playwright install chromium"
  },
  "dependencies": {
    "playwright": "{{ node_deps.playwright | default('^1') }}",
    "fingerprint-injector": "{{ node_deps.fingerprint_injector | default('^2') }}"
  }
}