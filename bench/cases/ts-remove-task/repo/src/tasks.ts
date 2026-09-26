import { removeAt } from "./list.ts";

export function completeTask(tasks: string[], index: number): string[] {
  return removeAt(tasks, index);
}
