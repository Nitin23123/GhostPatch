package com.tasks;

import java.util.ArrayDeque;
import java.util.Deque;

/** A first-in, first-out queue that holds at most {@code capacity} tasks. */
public class BoundedQueue {
    private final int capacity;
    private final Deque<String> tasks = new ArrayDeque<>();

    public BoundedQueue(int capacity) {
        this.capacity = capacity;
    }

    /** True when no more tasks fit. */
    public boolean isFull() {
        return tasks.size() >= capacity;
    }

    /** Adds a task at the back. Returns false, and adds nothing, when the queue is full. */
    public boolean offer(String task) {
        if (isFull()) {
            return false;
        }
        tasks.addLast(task);
        return true;
    }

    /** Takes the task at the front, or null when the queue is empty. */
    public String poll() {
        return tasks.pollFirst();
    }

    public int size() {
        return tasks.size();
    }
}
