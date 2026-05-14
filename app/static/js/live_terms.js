const tbody = document.querySelector("#terms-body");
const form = document.querySelector("#term-form");

async function loadTerms() {
  const response = await fetch("/api/terms");
  const terms = await response.json();
  tbody.innerHTML = terms
    .map(
      (term) => `
      <tr>
        <td>${term.id}</td>
        <td>${term.category}</td>
        <td>${term.source.join(", ")}</td>
        <td>${term.zh_cn}</td>
        <td><button data-id="${term.id}">Delete</button></td>
      </tr>
    `,
    )
    .join("");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const payload = {
    id: data.get("id"),
    category: data.get("category"),
    source: data.get("source").split(",").map((item) => item.trim()).filter(Boolean),
    aliases: data.get("aliases").split(",").map((item) => item.trim()).filter(Boolean),
    zh_cn: data.get("zh_cn"),
    priority: Number(data.get("priority") || 0),
    forbidden_translations: data.get("forbidden_translations").split(",").map((item) => item.trim()).filter(Boolean),
    asr_mishearings: data.get("asr_mishearings").split(",").map((item) => item.trim()).filter(Boolean),
  };
  await fetch("/api/terms", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  form.reset();
  await loadTerms();
});

tbody.addEventListener("click", async (event) => {
  if (event.target.matches("button[data-id]")) {
    await fetch(`/api/terms/${encodeURIComponent(event.target.dataset.id)}`, { method: "DELETE" });
    await loadTerms();
  }
});

document.querySelector("#reload").addEventListener("click", async () => {
  await fetch("/api/terms/reload", { method: "POST" });
  await loadTerms();
});

loadTerms();

