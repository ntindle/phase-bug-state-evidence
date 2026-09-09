export function CardImage({ cardName }: { cardName?: string }) {
  return (
    <div
      data-testid="card-image"
      style={{
        width: 120,
        height: 168,
        background: "#223",
        color: "#fff",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: 8,
      }}
    >
      {cardName ?? "card"}
    </div>
  );
}
