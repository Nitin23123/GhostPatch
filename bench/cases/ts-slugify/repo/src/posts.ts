import { slugify } from "./slug.ts";

export interface Post {
  id: number;
  title: string;
}

export const postUrl = (post: Post): string => `/blog/${post.id}/${slugify(post.title)}`;

export function sitemap(posts: Post[]): string[] {
  return posts.map(postUrl);
}
