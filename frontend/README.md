# NASA-ACEs frontend

React + ArcGIS map for the NASA-ACEs backend. Product context: [PRODUCT.md](PRODUCT.md). Design system: [DESIGN.md](DESIGN.md).

```bash
npm install
npm run dev                                        # proxies /api to the deployed backend
BACKEND_URL=http://127.0.0.1:8000 npm run dev      # or to a local one
npm run build && npm run lint
```

## Model map (fuel risk, NISAR)

The **NISAR** tab of the left panel (**Fuel risk** with the synthetic demo) shows the model result of the backend: its layers, legend, the values of a clicked cell
and the one-hour spread vectors. Everything comes from the result's metadata, nothing is hard-coded.

- The app asks for the result `latest`: the newest model result of a backend started with `RESULTS_DIR`
  (backend branch `feat/real-results`), or the synthetic demo when the backend has none. A backend without the
  `latest` alias (today's `prototype`) answers 404 and the app falls back to `forest-demo-v1`, as before.
- `VITE_BURNABILITY_RESULT=forest-demo-v1` (or any result id) pins one result.
- With a model result the picker groups its layers (Risk, NISAR, Spread, Fuel), the panel shows the map day,
  the model version and how much NISAR is behind the map, and "SYNTHETIC DEMO" disappears from the layer title.
- **Fire growth** (shown when the result's metadata has `capabilities.spread`): switch it on, click the map, and
  the app draws where a fire started there would be after 1, 3 and 6 hours, with the areas, the head fire and the
  wind used. The wind is the map day's or one you type (m/s at 10 m, the direction it blows from); "Fuel age"
  switches the slowing inside recent fire scars on and off. It is the backend's `GET .../spread`: surface-fire
  potential for one wind and the map day's fuels, no suppression; not a forecast.
- Fire cards and report rows show a fuel-risk class when the backend sends one (`FUEL_RISK_RESULT=latest`).
- `node scripts/check-backend.mjs http://127.0.0.1:8000` requests the endpoints with the URLs the app builds and
  checks the fields it reads.

---

Scaffold notes: this template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.
