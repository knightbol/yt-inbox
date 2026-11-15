/* Build-only tool: embed our SQL schema without requiring Python or xxd. */
#include <stdio.h>
int main(int argc, char **argv)
{
    if (argc != 2)
        return 1;
    printf("static const char %s[] =\n\"", argv[1]);
    int c;
    while ((c = getchar()) != EOF) {
        if (c == '\n')
            fputs("\\n\"\n\"", stdout);
        else if (c == '"' || c == '\\')
            printf("\\%c", c);
        else
            printf("%c", c);
    }
    puts("\";");
    return ferror(stdin) || ferror(stdout);
}
