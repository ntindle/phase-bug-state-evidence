import React from "react";

// Minimal passthrough for the modal chrome; the animation under test lives in
// NamedChoiceModal's ButtonGrid pills, not in the overlay.
export function ChoiceOverlay({
  title,
  subtitle,
  children,
  footer,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div data-testid="choice-overlay">
      <h1 data-testid="overlay-title">{title}</h1>
      {subtitle ? <p data-testid="overlay-subtitle">{subtitle}</p> : null}
      {children}
      {footer}
    </div>
  );
}

export function ConfirmButton({
  onClick,
  disabled,
  label = "Confirm",
}: {
  onClick?: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} data-testid="confirm">
      {label}
    </button>
  );
}
