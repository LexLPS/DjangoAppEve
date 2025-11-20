import requests
from django.conf import settings

SALEOR_GRAPHQL_URL = getattr(settings, "SALEOR_GRAPHQL_URL", "http://localhost:8000/graphql/")

def fetch_products():
    query = """
    query {
      products(first: 20, channel: "default-channel") {
        edges {
          node {
            id
            name
            slug
            description
            thumbnail {
              url
            }
            pricing {
              priceRange {
                start { gross { amount currency } }
                stop  { gross { amount currency } }
              }
            }
          }
        }
      }
    }
    """
    resp = requests.post(SALEOR_GRAPHQL_URL, json={"query": query})
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["products"]["edges"]