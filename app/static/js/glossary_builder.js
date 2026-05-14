const form = document.querySelector("#builder-form");
const output = document.querySelector("#builder-output");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const payload = {
    game_id: data.get("game_id"),
    zh_cn_title: data.get("zh_cn_title"),
    sources: [
      {
        type: data.get("source_type"),
        name: "manual",
        content: data.get("content"),
        url: data.get("content"),
      },
    ],
    target_categories: ["game_title", "character", "item", "skill", "map", "event", "system"],
  };
  const response = await fetch("/api/glossary-builder/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  output.textContent = JSON.stringify(await response.json(), null, 2);
});

