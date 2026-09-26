export function removeAt<T>(items: T[], index: number): T[] {
  const copy = [...items];
  copy.splice(index, 1);
  return copy;
}
