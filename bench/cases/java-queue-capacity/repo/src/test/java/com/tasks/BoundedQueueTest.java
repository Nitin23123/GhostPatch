package com.tasks;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import org.junit.jupiter.api.Test;

class BoundedQueueTest {
    @Test
    void firstInFirstOut() {
        BoundedQueue queue = new BoundedQueue(5);
        queue.offer("a");
        queue.offer("b");
        assertEquals("a", queue.poll());
        assertEquals("b", queue.poll());
    }

    @Test
    void pollingAnEmptyQueue() {
        assertNull(new BoundedQueue(1).poll());
    }
}
