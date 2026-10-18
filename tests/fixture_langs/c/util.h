#ifndef UTIL_H
#define UTIL_H

#define MAX_ITEMS 16

struct item {
    int id;
    const char *name;
};

typedef struct item item_t;

enum color { RED, GREEN };

int helper(int x);

#endif
