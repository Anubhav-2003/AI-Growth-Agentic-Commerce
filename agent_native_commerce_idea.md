# Agent-Native Commerce Layer

- Online stores today are designed mainly for humans, using HTML pages, menus, buttons, and visual navigation.

- AI agents can use those stores today, but usually through vision-based browsing or a limited set of MCP/API tools.

- Our idea is to create a **standard agent-readable version of an e-commerce store**.

- The agent should be able to directly explore products, variants, prices, inventory, policies, reviews, and other store data in a structured format.

- This should feel like a **searchable website for AI agents**, rather than a normal website built from HTML.

- The agent should not depend on opaque tools such as `search_products()` just to understand what exists in the store.

- Tools should mainly be used for actions such as **add to cart, checkout, cancel, return, or track an order**.

- Existing Shopify, WooCommerce, Magento, and custom stores should be able to connect to this layer without rebuilding their existing backend.

- A common format would allow ChatGPT, Claude, Gemini, and other agents to understand different stores in the same way.

- In simple terms: **HTML made websites readable by browsers; this layer would make e-commerce stores readable by AI agents.**
