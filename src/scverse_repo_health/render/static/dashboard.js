// Dashboard interactions. Plain DOM, no build step — the site is served as static files.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  /* -- theme toggle -------------------------------------------------------------- */

  const toggle = $("theme-toggle");
  if (toggle) {
    const order = ["auto", "light", "dark"];
    toggle.addEventListener("click", () => {
      const current = document.documentElement.dataset.theme || "auto";
      const next = order[(order.indexOf(current) + 1) % order.length];
      document.documentElement.dataset.theme = next;
      if (next === "auto") localStorage.removeItem("repo-health-theme");
      else localStorage.setItem("repo-health-theme", next);
      toggle.title = `Colour theme: ${next}`;
    });
  }

  const matrix = document.querySelector("table.matrix");
  if (!matrix) return;

  /* -- row filtering -------------------------------------------------------------- */

  const search = $("search");
  const groupFilter = $("group-filter");
  const failuresOnly = $("failures-only");
  const requiredOnly = $("required-only");
  const counter = $("visible-count");
  const emptyState = $("empty-state");
  const rows = Array.from(matrix.querySelectorAll("tbody tr[data-repo]"));
  const sections = Array.from(matrix.querySelectorAll("tbody"));

  function applyFilters() {
    const needle = (search?.value || "").trim().toLowerCase();
    const group = groupFilter?.value || "";
    const onlyFailing = failuresOnly?.checked;
    let visible = 0;

    for (const row of rows) {
      const matchesText = !needle || row.dataset.repo.toLowerCase().includes(needle);
      const matchesGroup = !group || row.dataset.category === group;
      const matchesFailing = !onlyFailing || row.dataset.failing === "1";
      const show = matchesText && matchesGroup && matchesFailing;
      row.hidden = !show;
      if (show) visible += 1;
    }

    // Hide a whole section once every repo in it is filtered out.
    for (const section of sections) {
      const any = Array.from(section.querySelectorAll("tr[data-repo]")).some((r) => !r.hidden);
      section.hidden = !any;
    }

    if (counter) counter.textContent = `${visible} of ${rows.length} repositories`;
    if (emptyState) emptyState.hidden = visible !== 0;
  }

  search?.addEventListener("input", applyFilters);
  groupFilter?.addEventListener("change", applyFilters);
  failuresOnly?.addEventListener("change", applyFilters);

  /* -- tier filtering -------------------------------------------------------------- */

  requiredOnly?.addEventListener("change", () => {
    const on = requiredOnly.checked;
    for (const el of matrix.querySelectorAll('[data-tier]')) {
      el.hidden = on && el.dataset.tier !== "required";
    }
  });

  /* -- collapsible column groups ------------------------------------------------------ */

  const collapsed = new Set();

  function paintGroups() {
    matrix.classList.toggle("has-collapsed", collapsed.size > 0);
    for (const el of matrix.querySelectorAll("[data-group]")) {
      el.classList.toggle("is-collapsed", collapsed.has(el.dataset.group));
    }
    for (const button of matrix.querySelectorAll(".colgroup-toggle")) {
      const isCollapsed = collapsed.has(button.dataset.group);
      // The expanded and collapsed headers are two different buttons; only one shows.
      button.setAttribute("aria-expanded", String(!isCollapsed));
    }
    const all = $("collapse-all");
    if (all) {
      const everyGroup = new Set(
        Array.from(matrix.querySelectorAll(".colgroup-toggle")).map((b) => b.dataset.group),
      );
      all.textContent = collapsed.size >= everyGroup.size ? "Expand all groups" : "Collapse all groups";
    }
  }

  matrix.addEventListener("click", (event) => {
    const button = event.target.closest(".colgroup-toggle");
    if (!button) return;
    const group = button.dataset.group;
    if (collapsed.has(group)) collapsed.delete(group);
    else collapsed.add(group);
    paintGroups();
  });

  $("collapse-all")?.addEventListener("click", () => {
    const everyGroup = Array.from(matrix.querySelectorAll(".colgroup-toggle")).map((b) => b.dataset.group);
    if (collapsed.size >= new Set(everyGroup).size) collapsed.clear();
    else for (const g of everyGroup) collapsed.add(g);
    paintGroups();
  });

  applyFilters();
  paintGroups();
})();
