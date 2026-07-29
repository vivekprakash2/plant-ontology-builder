# UI Prototype (No Backend)

This is a frontend-only demo for the hackathon storyline.

## Run locally

1. Open the folder in VS Code.
2. Open `ui-prototype/index.html` in a browser.

If your browser blocks local script loading, run a simple local server:

```bash
cd ui-prototype
python3 -m http.server 8080
```

Then open:

- http://localhost:8080

## What is mocked

- Preset questions (S1, S4, S5)
- Root-cause summary + confidence badge
- Cross-system timeline
- Evidence cards with source references
- Alias/relationship mini-view

## Next integration steps

1. Replace `MOCK_CASES` in `mockData.js` with API responses.
2. Keep the same object shape so `app.js` does not need large changes.
3. Add a real endpoint call in `runButton` click handler.
