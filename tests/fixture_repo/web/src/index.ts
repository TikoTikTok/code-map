import { fetchUser, toName } from './api';
import type { User } from './types';

export class App {
  private users: User[] = [];

  /** Load one user into the list. */
  async run(id: number) {
    const u = await fetchUser(id);
    this.users.push(u);
    return toName(u);
  }
}

export function boot() {
  return new App().run(1);
}
