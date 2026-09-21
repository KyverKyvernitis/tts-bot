export function revealModuleField(fieldId: string) {
  window.requestAnimationFrame(() => {
    const flow = fieldId.match(/^tickets\.option_items\.([^.]+)\./)?.[1];
    if (flow) document.querySelector<HTMLButtonElement>(`[aria-controls="ticket-flow-panel-${flow}"][aria-expanded="false"]`)?.click();
    window.requestAnimationFrame(() => {
      const target = Array.from(document.querySelectorAll<HTMLElement>("[data-field-id]")).find(node => node.dataset.fieldId === fieldId);
      if (!target) return;
      for (let parent = target.parentElement; parent; parent = parent.parentElement) if (parent instanceof HTMLDetailsElement) parent.open = true;
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      target.querySelector<HTMLElement>("input:not(:disabled), textarea:not(:disabled), button:not(:disabled)")?.focus({ preventScroll: true });
    });
  });
}
