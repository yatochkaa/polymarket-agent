---
name: frontend-product-quality
description: Use when creating or reviewing product UI, forms, dashboards, data tables, navigation, responsive layouts, interaction states, accessibility, or frontend behavior. Improve usability and implementation quality while reusing the existing visual language. Do not use for backend-only work, marketing graphics, or a request limited to changing literal text.
---

# Frontend product quality

## Before changing UI

Inspect current components, tokens/styles, layout patterns, state management, API contracts, and representative screens. Reuse before adding a new pattern or dependency.

## MUST

- Preserve working behavior and data contracts.
- Provide loading, empty, error, disabled, success, and permission-denied states where relevant.
- Use semantic controls, visible labels, keyboard access, and visible focus.
- Never communicate status by color alone.
- Keep destructive actions explicit and recoverable where possible.
- Make validation errors specific, associated with their fields, and understandable.
- Check narrow and wide layouts; avoid hiding essential actions on small screens.
- Keep frontend authorization hints subordinate to backend enforcement.

## SHOULD

- Prioritize hierarchy, spacing, typography, and clarity before decoration.
- Reduce visual noise in dense operational screens.
- Keep primary actions obvious and dangerous actions separated.
- Use progressive disclosure for advanced options.
- Prefer familiar product patterns over novel interaction for its own sake.

## MUST NOT

- Invent backend endpoints or response fields.
- add a UI library for one component without approval;
- replace functioning screens with static mockups;
- add excessive animation, gradients, glass effects, or ornamental dashboards;
- sacrifice accessibility for appearance.

## Verification

Review interaction states, keyboard flow, responsive behavior, API/type compatibility, and the available frontend checks. State what was visually inspected versus inferred.
