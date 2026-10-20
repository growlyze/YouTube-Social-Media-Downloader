// Settings form: collect values and save via the API.
"use strict";

const form = document.getElementById("settings-form");
const status = document.getElementById("settings-status");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const input of form.querySelectorAll("input[name], select[name]")) {
    if (input.type === "checkbox") body[input.name] = input.checked;
    else if (input.type === "number") body[input.name] = Number(input.value);
    else body[input.name] = input.value;
  }
  status.textContent = "Saving…";
  try {
    const { settings } = await api("/api/settings", { method: "PUT", body });
    if (form.elements.download_dir) form.elements.download_dir.value = settings.download_dir;
    status.textContent = "Saved.";
    toast("Settings saved.");
  } catch (err) {
    status.textContent = "";
    toast(err.message, "error");
  }
});
