import re

lineList = []
matchPattern = re.compile(r'st')
file = open('image.txt', 'r', encoding='UTF-8')
while 1:
    line = file.readline()
    if not line:
        print("Read file End or Error")
        break
    elif matchPattern.search(line):
        pass
    else:
        lineList.append(line)
file.close()
file = open(r'image.txt', 'w', encoding='UTF-8')
for i in lineList:
    file.write(i)
file.close()