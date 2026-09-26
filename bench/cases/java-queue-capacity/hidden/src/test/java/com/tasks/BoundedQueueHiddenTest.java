package com.tasks;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class BoundedQueueHiddenTest {
    @Test
    void refusesTasksBeyondItsCapacity() {
        BoundedQueue queue = new BoundedQueue(2);
        assertTrue(queue.offer("a"));
        assertTrue(queue.offer("b"));
        assertFalse(queue.offer("c"));
        assertEquals(2, queue.size());
    }

    @Test
    void fullAtCapacity() {
        BoundedQueue queue = new BoundedQueue(1);
        queue.offer("a");
        assertTrue(queue.isFull());
        queue.poll();
        assertFalse(queue.isFull());
    }
}
