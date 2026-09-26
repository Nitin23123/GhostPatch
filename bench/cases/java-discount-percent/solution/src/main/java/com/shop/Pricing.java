package com.shop;

/** Prices are in cents. */
public final class Pricing {
    private Pricing() {}

    /** The price after taking {@code percent} percent off. The discount is rounded down to a whole cent. */
    public static int applyDiscount(int cents, int percent) {
        if (percent <= 0) {
            return cents;
        }
        return cents - cents * percent / 100;
    }

    /** The total of a cart's prices after a cart-wide discount. */
    public static int cartTotal(int[] prices, int percent) {
        int total = 0;
        for (int price : prices) {
            total += applyDiscount(price, percent);
        }
        return total;
    }
}
