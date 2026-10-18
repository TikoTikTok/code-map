#include <stdio.h>
#include "util.h"

int main(int argc, char **argv) {
    item_t it = {1, "x"};
    printf("%d %s\n", helper(it.id), argv[0]);
    return argc > MAX_ITEMS ? 1 : 0;
}
