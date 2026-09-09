export function objectImageProps(obj: { name?: string }) {
  return { cardName: obj?.name ?? "card" };
}
