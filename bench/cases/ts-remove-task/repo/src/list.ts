export function removeAt<T>(items: T[], index: number): T[] {
  const copy = [...items];
  copy.splice(index);
  return copy;
}
