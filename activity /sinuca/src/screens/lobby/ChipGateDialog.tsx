import type { ReactNode } from "react";

export type ChipGateDialogKind = "debt" | "negative";

export type ChipGateDialogViewModel = {
  kind: ChipGateDialogKind;
  title: string;
  resultingChips: number;
};

type ChipGateDialogProps = {
  dialog: ChipGateDialogViewModel | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

export default function ChipGateDialog({ dialog, busy, onCancel, onConfirm }: ChipGateDialogProps) {
  if (!dialog) return null;

  const prefix: ReactNode = dialog.kind === "negative"
    ? "Se continuar, seu saldo ficará em "
    : "Seu saldo ficará em ";

  return (
    <div className="activity-confirm" role="dialog" aria-modal="true" aria-live="polite">
      <div className="activity-confirm__backdrop" onClick={() => { if (!busy) onCancel(); }} />
      <div className={`activity-confirm__panel activity-confirm__panel--${dialog.kind}`}>
        <div className="activity-confirm__panel-bg" aria-hidden="true" />
        <div className="activity-confirm__content">
          <div className="activity-confirm__title">{dialog.title}</div>
          <div className="activity-confirm__body">
            {prefix}
            <span className="activity-confirm__debt-value">-{Math.abs(dialog.resultingChips)} fichas</span>
            .
          </div>
          <div className="activity-confirm__actions">
            <button type="button" className="activity-confirm__button activity-confirm__button--ghost" disabled={busy} onClick={onCancel}>
              Melhor não...
            </button>
            <button type="button" className="activity-confirm__button activity-confirm__button--danger" disabled={busy} onClick={onConfirm}>
              <span>Sim (ficar com </span>
              <span className="activity-confirm__debt-value">-{Math.abs(dialog.resultingChips)}</span>
              <span> fichas)</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
