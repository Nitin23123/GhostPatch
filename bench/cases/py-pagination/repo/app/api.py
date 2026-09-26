from app.pagination import page_count, paginate


def list_products(products, page=1):
    return {
        "page": page,
        "pages": page_count(len(products)),
        "items": paginate(products, page),
    }
