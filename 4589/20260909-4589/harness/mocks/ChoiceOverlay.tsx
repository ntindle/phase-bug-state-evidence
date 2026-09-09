import type { ReactNode } from "react";

// Faithful chrome mock: layout only. All interaction logic (reorder state,
// confirm dispatch) lives in the real libraryModals.tsx under test.
export function ChoiceOverlay({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div data-testid="choice-overlay">
      <h2 data-testid="overlay-title">{title}</h2>
      <p data-testid="overlay-subtitle">{subtitle}</p>
      <div>{children}</div>
      <div>{footer}</div>
    </div>
  );
}

export function ConfirmButton({
  onClick,
  disabled = false,
  label,
}: {
  onClick: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button data-testid="confirm" onClick={onClick} disabled={disabled}>
      {label ?? "Confirm"}
    </button>
  );
}

export function CancelButton({
  onClick,
  label,
}: {
  onClick: () => void;
  label?: string;
}) {
  return (
    <button data-testid="cancel" onClick={onClick}>
      {label ?? "Cancel"}
    </button>
  );
}

export function ScrollableCardStrip({ children }: { children: ReactNode }) {
  return <div data-testid="card-strip">{children}</div>;
}
